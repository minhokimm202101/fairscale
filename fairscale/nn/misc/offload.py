# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the BSD license found in the
# LICENSE file in the root directory of this source tree.

"""
A wrapper which streams the model in and out of the GPU automatically during FW and optionally BW passes
(Can be used for inference only)
"""

from builtins import isinstance
import logging
from typing import Any, List, Tuple

import torch
from torch import nn
from torch.cuda.amp import custom_bwd, custom_fwd


def _split(modules: nn.Sequential, number_splits: int) -> List[List[nn.Module]]:
    number_splits = min(len(modules), number_splits)
    splits: List[List[nn.Module]] = [[] for _ in range(number_splits)]

    # Count the number of parameters per exposed layer, use that as a proxy for memory footprint
    total_number_params = sum([sum(p.numel() for p in m.parameters()) for m in modules])
    number_parameters_per_shard = total_number_params // number_splits

    current_shard = 0

    logging.info(
        f"This model has {total_number_params/1e6:.2f}M parameters, aiming for {number_parameters_per_shard/1e6:.2f}M parameters per shard"
    )

    for m in modules:
        # Number of parameters in the current shard
        current_shard_params = sum(p.numel() for sm in splits[current_shard] for p in sm.parameters())

        # This shard is big enough, point to the next one
        if (
            current_shard_params > 0
            and current_shard_params + sum(p.numel() for p in m.parameters()) > number_parameters_per_shard
            and current_shard < number_splits - 1
        ):
            current_shard += 1

        splits[current_shard].append(m)

    for i, split in enumerate(splits):
        current_shard_params = sum(p.numel() for sm in split for p in sm.parameters())
        logging.info(f"Shard {i} holds {current_shard_params/1e6:.2f}M parameters")

    return splits


class ModelShard(nn.Module):
    """
    Wrap one shard of the model, make it possible to load parameters on the 
    fly for the FW and BW pass on the given device.
    """

    def __init__(
        self, cpu_model_shard: nn.Module, device: torch.device, offload_device: torch.device, index: int,
    ):
        super().__init__()
        self.model_shard = cpu_model_shard
        self.index = index

        # Save all the parameter sizes to be able to restore them
        self.device = device
        torch.cuda.device(self.device)

        self.offload_device = offload_device

        self.model_shard.to(offload_device)
        self.cuda_stream = torch.cuda.Stream(
            device=self.device
        )  # needed to make sure load/offload really run in parallel with compute

    def forward(self, *inputs):  # type: ignore
        return self.model_shard(*inputs) if isinstance(inputs, tuple) else self.model_shard(inputs)

    def to(self, device: torch.device) -> "ModelShard":  # type: ignore
        # Make sure that the lookahead and lookback shards are not captured by this call
        self.model_shard.to(device)
        return self

    def train(self, mode: bool = True) -> "ModelShard":
        # Make sure that the lookahead and lookback shards are not captured by this call
        self.model_shard.train(mode)
        return self

    def to_device(self) -> None:
        self.model_shard.to(device=self.device, non_blocking=True)

    def forward_load(self, non_blocking: bool = True) -> None:
        with torch.cuda.stream(self.cuda_stream):
            # Restore all the parameter buffers
            self.model_shard.to(device=self.device, non_blocking=non_blocking)

    def backward_load(self, non_blocking: bool = True) -> None:
        with torch.cuda.stream(self.cuda_stream):
            self.model_shard.to(self.device, non_blocking=non_blocking)

    def forward_drop(self, non_blocking: bool = True) -> None:
        with torch.cuda.stream(self.cuda_stream):
            self.model_shard.to(self.offload_device, non_blocking=non_blocking)

    def backward_drop(self, non_blocking: bool = True) -> None:
        with torch.cuda.stream(self.cuda_stream):
            self.model_shard.to(self.offload_device, non_blocking=non_blocking)


class ActivationCheckpointing(torch.autograd.Function):
    """
     This Function enables us to override the forward and backward pass of the nn.Module.

     - In the FW pass, it drops parameters in the previous shard and
     loads parameters for the next shard. No graph is constructed in the FW pass.

     - In the BW pass, it does the reverse. We run the forward pass and
     calculate gradients as needed. The trade-off is latency vs memory.

     - Follows heavily from https://pytorch.org/docs/stable/_modules/torch/utils/checkpoint.html#checkpoint.

     NOTE: see https://pytorch.org/docs/stable/autograd.html#torch.autograd.Function
     """

    @staticmethod
    @custom_fwd
    def forward(ctx: Any, inputs: Any, model_instance: Any) -> Any:
        inputs = inputs if isinstance(inputs, tuple) else (inputs,)

        ctx.inputs = inputs
        ctx.model_instance = model_instance
        # TODO(anj-s): We might need to store this for each boundary activation.
        # Currently we assume all boundary activation inputs require
        ctx.grad_requirements = tuple(x.requires_grad for x in inputs)
        ctx.fwd_rng_state = torch.get_rng_state()

        # List of input activations starting with the given input.
        model_instance._activations = [inputs]
        # Enumerate through layer shards and apply activations from the previous shard.
        for index, layer_shard in enumerate(model_instance.model_slices):
            # Bring in the current activations onto the device.
            model_instance._activations[index] = tuple([a.cuda() for a in list(model_instance._activations[index])])
            # Bring in the current layer shard onto the device.
            layer_shard.forward_load()
            # Apply the FP and store the activations on the CPU.
            with torch.no_grad():
                inputs = model_instance._activations[index]
                output_list = []
                for given_input in inputs:
                    given_input_list = torch.chunk(given_input, model_instance._num_microbatches)
                    given_output_list = []
                    for inputs in given_input_list:
                        output = layer_shard(inputs)
                        given_output_list.append(output)
                    given_output = torch.cat(given_output_list).squeeze(-1)
                    output_list.append(given_output)
                output = tuple(output_list)
            output = output if isinstance(output, tuple) else (output,)
            model_instance._activations.append(tuple([a.cpu() for a in list(output)]))
            # Move the layer shard back to the CPU.
            layer_shard.forward_drop()

        # Move the output to the device since the user is expecting the output on the device.
        # TODO(anj-s): Check device to make sure the outputs and targets match device.
        model_instance._activations[-1] = tuple([a.cuda() for a in list(model_instance._activations[-1])])
        result = model_instance._activations[-1]
        return result[0] if len(result) == 1 else result

    @staticmethod
    @custom_bwd
    def backward(ctx, *grad_outputs):  # type: ignore
        if not torch.autograd._is_checkpoint_valid():
            raise RuntimeError("Checkpointing is not compatible with .grad(), please use .backward() if possible")

        inputs = ctx.inputs
        model_instance = ctx.model_instance

        for i, need_grad in enumerate(ctx.grad_requirements):
            inputs[i].requires_grad = need_grad

        all_grads = [grad_outputs]

        final_index = len(model_instance._activations) - 1

        for model_shard, activation in zip(
            reversed(model_instance.model_slices), reversed(model_instance._activations[:-1])
        ):
            # Move the activation to the device.
            activation = tuple([a.cuda() for a in list(activation)])
            # One of the inputs to the FW pass must require grad.
            for a in activation:
                a.requires_grad = True

            # Move the model shard to the device.
            model_shard.backward_load()
            # Store the BW pass state.
            bwd_rng_state = torch.get_rng_state()

            # TODO(anj-s): Why detach inputs?
            activation = torch.utils.checkpoint.detach_variable(activation)
            # Get the last gradient calculation.
            final_grads = all_grads[-1]
            if isinstance(activation, torch.Tensor):
                activation = (activation,)
            if isinstance(final_grads, torch.Tensor):
                final_grads = (final_grads,)
            # Iterate through all the inputs/outputs of a shard (there could be multiple).
            chunked_grad_list = []
            # Chunk the activation and grad based on the number of microbatches that are set.
            for chunked_activation, chunked_grad in zip(
                torch.chunk(*activation, model_instance._num_microbatches),
                torch.chunk(*final_grads, model_instance._num_microbatches),
            ):
                # Set the states to what it used to be before the forward pass.
                torch.set_rng_state(ctx.fwd_rng_state)

                if isinstance(chunked_activation, torch.Tensor):
                    chunked_activation = (chunked_activation,)
                if isinstance(chunked_grad, torch.Tensor):
                    chunked_grad = (chunked_grad,)

                # Since we need a grad value of a non leaf element we need to set these properties.
                for a in chunked_activation:
                    a.requires_grad = True
                    a.retain_grad()

                with torch.enable_grad():
                    # calculate the output of the last shard wrt to the stored activation at the slice boundary.
                    outputs = model_shard(*chunked_activation)

                # Set the states back to what it was at the start of this function.
                torch.set_rng_state(bwd_rng_state)
                torch.autograd.backward(outputs, chunked_grad)
                chunked_grad_list += [a.grad for a in chunked_activation]

            # Append the list of grads to the all_grads list and this should be on the CPU.
            all_grads.append(torch.cat(chunked_grad_list).squeeze(-1))
            # Move activation back to the CPU.
            # TODO(anj-s): Why does moving activations to CPU cause the .grad property to be None?
            activation = tuple([a.cpu() for a in list(activation)])
            # Move the shard back to the CPU.
            model_shard.backward_drop()

        detached_inputs = model_instance._activations[0]
        grads = tuple(inp.grad if isinstance(inp, torch.Tensor) else inp for inp in detached_inputs)
        return (None, None) + grads


class ShardSyncLayer(torch.autograd.Function):
    """
     The shard sync layer is a synchronization point between model shards.

     - In the forward pass, it drops parameters in the previous shard and
     loads parameters for the next shard.

     - In the backward pass, it does the reverse.

     It does not change or create any outputs at all, instead it just
     forwards the input as the output.

     NOTE: see https://pytorch.org/docs/stable/autograd.html#torch.autograd.Function
     """

    @staticmethod
    @custom_fwd
    def forward(ctx: Any, inputs: Any, index: int, model_slices: Any, model_instance: Any) -> Any:
        drop_index = index
        load_index = index + 1
        max_slices = len(model_slices)

        if drop_index >= 0:
            # Move shard from device to offload device.
            logging.info(f"Dropping shard {drop_index}")
            model_slices[drop_index].forward_drop()

        if load_index < max_slices:
            # Load shard from offload device to device.
            logging.info(f"Loading shard{load_index}")
            model_slices[load_index].forward_load()

        ctx.index = index
        ctx.model_slices = model_slices
        ctx.model_instance = model_instance

        return inputs if isinstance(inputs, tuple) else (inputs,)

    @staticmethod
    @custom_bwd
    def backward(ctx, *grad_outputs):  # type: ignore

        load_index = ctx.index
        drop_index = load_index + 1
        model_slices = ctx.model_slices
        model_instance = ctx.model_instance

        # TODO(anj-s): Are these redundant in the backward pass?
        if drop_index == len(model_slices):
            # Drop the last activation since it is still on the CPU
            # after the loss.backward() call.
            model_instance._activations[-1] = tuple([a.cuda() for a in list(model_instance._activations[-1])])

        if drop_index < len(model_slices):
            # Move shard from device to offload device.
            logging.info(f"Backward Dropping shard {drop_index}")
            model_slices[drop_index].backward_drop()
            model_instance._activations[drop_index] = tuple(
                [a.cpu() for a in list(model_instance._activations[drop_index])]
            )

        if load_index >= 0:
            # Load shard from offload device to device.
            logging.info(f"Backward Loading shard{load_index}")
            model_slices[load_index].backward_load()
            model_instance._activations[load_index] = tuple(
                [a.cuda() for a in list(model_instance._activations[load_index])]
            )

        # The returned variables need to mirror the forward inputs
        # TODO(anj-s): Why do we need to do this?
        if isinstance(grad_outputs, tuple):
            return grad_outputs[0], None, None, None

        return grad_outputs, None, None, None


class OffloadModel(nn.Module):
    """Implements training with optimizer state sharding and model sharding.

    This experiments with a different way to get to the full zero suite
    The model is sharded, then the normal distributed data parallel algorithm can be used on a per-model shard basis.
    Each shard is offloaded and loaded following a compute wavefront, during the forward and backward pass.

    Each model shard can be updated by a normal pytorch optimizer.

    Args:
        module (~torch.nn.Sequential): module to be parallelized
        optimizer (~torch.optim.Optimizer): optimizer to be used for training
        optimizer_params(Dict): extra parameters for the optimizer

        device (torch.device):
            device where the active model should reside

        offload_device (torch.device):
            device where the inactive model should reside

        n_slices (int):
            number of slices in which to decomppose the model
    """

    def __init__(
        self,
        model_cpu: nn.Sequential,  # hard pre-requisite for now, easier model slicing
        device: torch.device,
        offload_device: torch.device = torch.device("cpu"),
        n_slices: int = 5,
        checkpoint_activation=False,
        num_microbatches: int = 1,
    ):
        super().__init__()
        # TODO(anj-s): Add error checks for cuda and sequential model.

        self.device = device
        self.offload_device = offload_device

        # Slice the model into roughly equivalent sequential shards.
        splits = _split(model_cpu, n_slices)

        # List of model shards that will be placed on/off the device.
        self.model_slices: List[nn.Module] = []

        for i, split in enumerate(splits):
            # Add one model handling this slice
            self.model_slices.append(
                ModelShard(
                    cpu_model_shard=nn.Sequential(*split), device=device, offload_device=offload_device, index=i,
                )
            )

        # Expose a unified view of the slices
        self.model = torch.nn.Sequential(*self.model_slices)

        # intermediate activations at the slice boundaries.
        self._activations: List[Tuple] = []

        # Currently we only support microbatches with activation checkpointing.
        if not checkpoint_activation and num_microbatches > 1:
            raise RuntimeError("We currently only support microbatches with activation checkpointing.")

        # Bool indicating if we want to checkpoint activation on the host.
        self._checkpoint_activation = checkpoint_activation

        # Number of microbatches to run per batch on the device
        self._num_microbatches = num_microbatches

    def forward(self, *inputs: Any, **_: Any) -> Any:
        # At least one of the inputs needs to have `requires_grad` set.
        # TODO(anj-s): Should we require users to set this or should we set it here?
        for inp in inputs:
            inp.requires_grad = True
        if self._checkpoint_activation:
            return ActivationCheckpointing.apply(*inputs, self)

        shardSync = ShardSyncLayer.apply
        self._activations = []
        for index in range(-1, len(self.model_slices)):
            if index >= 0:
                # TODO(anj-s): This might be a redundant call since we have the previous
                # activation on the device already.
                self._activations[index] = tuple([a.cuda() for a in list(self._activations[index])])
                inputs = self._activations[index]
                inputs = self.model_slices[index](*inputs)
            # Call the custom autograd hooks (discard/load slices FW and BW)
            inputs = shardSync(inputs, index, self.model_slices, self)
            self._activations.append(inputs)
            if index >= 0:
                self._activations[index] = tuple([a.cpu() for a in list(self._activations[index])])

        # We don't move the last activation/output since the target is present
        # on the device.
        # TODO(anj-s): It is now a requirement that the target tensors be placed on the
        # device.
        result = self._activations[-1]
        return result[0] if len(result) == 1 else result