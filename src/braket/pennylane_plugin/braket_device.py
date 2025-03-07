# Copyright Amazon.com Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You
# may not use this file except in compliance with the License. A copy of
# the License is located at
#
#     http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF
# ANY KIND, either express or implied. See the License for the specific
# language governing permissions and limitations under the License.

"""
Devices
=======

**Module name:** :mod:`braket.pennylane_braket.braket_device`

.. currentmodule:: braket.pennylane_braket.braket_device

Braket devices to be used with PennyLane

Classes
-------

.. autosummary::
   BraketAwsQubitDevice
   BraketLocalQubitDevice

Code details
~~~~~~~~~~~~
"""

# pylint: disable=invalid-name
from enum import Enum, auto
from typing import FrozenSet, Iterable, List, Optional, Sequence, Union

from braket.aws import AwsDevice, AwsDeviceType, AwsQuantumTask, AwsQuantumTaskBatch, AwsSession
from braket.circuits import Circuit, Instruction
from braket.device_schema import DeviceActionType
from braket.devices import Device, LocalSimulator
from braket.simulator import BraketSimulator
from braket.tasks import GateModelQuantumTaskResult, QuantumTask
from pennylane import CircuitGraph, QuantumFunctionError, QubitDevice
from pennylane import numpy as np
from pennylane.measurements import Expectation, Probability, Sample, State, Variance
from pennylane.operation import Observable, Operation

from braket.pennylane_plugin.translation import (
    supported_operations,
    translate_operation,
    translate_result,
    translate_result_type,
)

from ._version import __version__

RETURN_TYPES = [Expectation, Variance, Sample, Probability, State]
MIN_SIMULATOR_BILLED_MS = 3000


class Shots(Enum):
    """Used to specify the default number of shots in BraketAwsQubitDevice"""

    DEFAULT = auto()


class BraketQubitDevice(QubitDevice):
    r"""Abstract Amazon Braket qubit device for PennyLane.

    Args:
        wires (int or Iterable[Number, str]]): Number of subsystems represented by the device,
            or iterable that contains unique labels for the subsystems as numbers
            (i.e., ``[-1, 0, 2]``) or strings (``['ancilla', 'q1', 'q2']``).
        device (Device): The Amazon Braket device to use with PennyLane.
        shots (int or None): Number of circuit evaluations or random samples included,
            to estimate expectation values of observables. If this value is set to ``None`` or
            ``0``, the device runs in analytic mode (calculations will be exact).
        **run_kwargs: Variable length keyword arguments for ``braket.devices.Device.run()`.
    """
    name = "Braket PennyLane plugin"
    pennylane_requires = ">=0.18.0"
    version = __version__
    author = "Amazon Web Services"

    def __init__(
        self,
        wires: Union[int, Iterable],
        device: Device,
        *,
        shots: Union[int, None],
        **run_kwargs,
    ):
        super().__init__(wires, shots=shots or None)
        self._device = device
        self._circuit = None
        self._task = None
        self._run_kwargs = run_kwargs
        self._supported_ops = supported_operations(self._device)
        self._check_supported_result_types()

    def reset(self):
        super().reset()
        self._circuit = None
        self._task = None

    @classmethod
    def capabilities(cls):
        """Add support for inverse"""
        capabilities = super().capabilities().copy()
        capabilities.update(supports_inverse_operations=True)
        return capabilities

    @property
    def operations(self) -> FrozenSet[str]:
        """FrozenSet[str]: The set of names of PennyLane operations that the device supports."""
        return self._supported_ops

    @property
    def observables(self) -> FrozenSet[str]:
        base_observables = frozenset(super().observables)
        if not self.shots:
            return base_observables.union({"Hamiltonian"})
        return base_observables

    @property
    def circuit(self) -> Circuit:
        """Circuit: The last circuit run on this device."""
        return self._circuit

    @property
    def task(self) -> QuantumTask:
        """QuantumTask: The task corresponding to the last run circuit."""
        return self._task

    def _pl_to_braket_circuit(self, circuit, **run_kwargs):
        """Converts a PennyLane circuit to a Braket circuit"""
        braket_circuit = self.apply(
            circuit.operations,
            rotations=None,  # Diagonalizing gates are applied in Braket SDK
            **run_kwargs,
        )
        for observable in circuit.observables:
            dev_wires = self.map_wires(observable.wires).tolist()
            translated = translate_result_type(observable, dev_wires, self._braket_result_types)
            if isinstance(translated, tuple):
                for result_type in translated:
                    braket_circuit.add_result_type(result_type)
            else:
                braket_circuit.add_result_type(translated)
        return braket_circuit

    def statistics(
        self, braket_result: GateModelQuantumTaskResult, observables: Sequence[Observable]
    ) -> Union[float, List[float]]:
        """Processes measurement results from a Braket task result and returns statistics.

        Args:
            braket_result (GateModelQuantumTaskResult): the Braket task result
            observables (List[Observable]): the observables to be measured

        Raises:
            QuantumFunctionError: if the value of :attr:`~.Observable.return_type` is not supported

        Returns:
            Union[float, List[float]]: the corresponding statistics
        """
        results = []
        for obs in observables:
            if obs.return_type not in RETURN_TYPES:
                raise QuantumFunctionError(
                    "Unsupported return type specified for observable {}".format(obs.name)
                )
            results.append(self._get_statistic(braket_result, obs))

        return results

    def _braket_to_pl_result(self, braket_result, circuit):
        """Calculates the PennyLane results from a Braket task result. A PennyLane circuit
        also determines the output observables."""
        # Compute the required statistics
        results = self.statistics(braket_result, circuit.observables)

        # Ensures that a combination with sample does not put
        # single-number results in superfluous arrays
        all_sampled = all(obs.return_type is Sample for obs in circuit.observables)
        if circuit.is_sampled and not all_sampled:
            return np.asarray(results, dtype="object")

        return np.asarray(results)

    @staticmethod
    def _tracking_data(task):
        if task.state() == "COMPLETED":
            tracking_data = {"braket_task_id": task.id}
            try:
                simulation_ms = (
                    task.result().additional_metadata.simulatorMetadata.executionDuration
                )
                tracking_data["braket_simulator_ms"] = simulation_ms
                tracking_data["braket_simulator_billed_ms"] = max(
                    simulation_ms, MIN_SIMULATOR_BILLED_MS
                )
            except AttributeError:
                pass
            return tracking_data
        else:
            return {"braket_failed_task_id": task.id}

    def execute(self, circuit: CircuitGraph, **run_kwargs) -> np.ndarray:
        self.check_validity(circuit.operations, circuit.observables)
        self._circuit = self._pl_to_braket_circuit(circuit, **run_kwargs)
        self._task = self._run_task(self._circuit)
        braket_result = self._task.result()

        if self.tracker.active:
            tracking_data = self._tracking_data(self._task)
            self.tracker.update(executions=1, shots=self.shots, **tracking_data)
            self.tracker.record()

        return self._braket_to_pl_result(braket_result, circuit)

    def apply(
        self, operations: Sequence[Operation], rotations: Sequence[Operation] = None, **run_kwargs
    ) -> Circuit:
        """Instantiate Braket Circuit object."""
        rotations = rotations or []
        circuit = Circuit()

        # Add operations to Braket Circuit object
        for operation in operations + rotations:
            gate = translate_operation(operation)
            dev_wires = self.map_wires(operation.wires).tolist()
            ins = Instruction(gate, dev_wires)
            circuit.add_instruction(ins)

        unused = set(range(self.num_wires)) - {int(qubit) for qubit in circuit.qubits}

        # To ensure the results have the right number of qubits
        for qubit in sorted(unused):
            circuit.i(qubit)

        return circuit

    def _check_supported_result_types(self):
        supported_result_types = self._device.properties.action[
            "braket.ir.jaqcd.program"
        ].supportedResultTypes

        self._braket_result_types = frozenset(
            result_type.name for result_type in supported_result_types
        )

    def _run_task(self, circuit):
        raise NotImplementedError("Need to implement task runner")

    def _get_statistic(self, braket_result, observable):
        dev_wires = self.map_wires(observable.wires).tolist()
        return translate_result(braket_result, observable, dev_wires, self._braket_result_types)


class BraketAwsQubitDevice(BraketQubitDevice):
    r"""Amazon Braket AwsDevice qubit device for PennyLane.

    Args:
        wires (int or Iterable[Number, str]]): Number of subsystems represented by the device,
            or iterable that contains unique labels for the subsystems as numbers
            (i.e., ``[-1, 0, 2]``) or strings (``['ancilla', 'q1', 'q2']``).
        device_arn (str): The ARN identifying the ``AwsDevice`` to be used to
            run circuits; The corresponding AwsDevice must support quantum
            circuits via JAQCD. You can get device ARNs using ``AwsDevice.get_devices``,
            from the Amazon Braket console or from the Amazon Braket Developer Guide.
        s3_destination_folder (AwsSession.S3DestinationFolder): Name of the S3 bucket
            and folder, specified as a tuple.
        poll_timeout_seconds (float): Total time in seconds to wait for
            results before timing out.
        poll_interval_seconds (float): The polling interval for results in seconds.
        shots (int, None or Shots.DEFAULT): Number of circuit evaluations or random samples
            included, to estimate expectation values of observables. If set to Shots.DEFAULT,
            uses the default number of shots specified by the remote device. If ``shots`` is set
            to ``0`` or ``None``, the device runs in analytic mode (calculations will be exact).
            Analytic mode is not available on QPU and hence an error will be raised.
            Default: Shots.DEFAULT
        aws_session (Optional[AwsSession]): An AwsSession object created to manage
            interactions with AWS services, to be supplied if extra control
            is desired. Default: None
        parallel (bool): Indicates whether to use parallel execution for gradient calculations.
            Default: False
        max_parallel (int, optional): Maximum number of tasks to run on AWS in parallel.
            Batch creation will fail if this value is greater than the maximum allowed concurrent
            tasks on the device. If unspecified, uses defaults defined in ``AwsDevice``.
            Ignored if ``parallel=False``.
        max_connections (int): The maximum number of connections in the Boto3 connection pool.
            Also the maximum number of thread pool workers for the batch.
            Ignored if ``parallel=False``.
        max_retries (int): The maximum number of retries to use for batch execution.
            When executing tasks in parallel, failed tasks will be retried up to ``max_retries``
            times. Ignored if ``parallel=False``.
        **run_kwargs: Variable length keyword arguments for ``braket.devices.Device.run()``.
    """
    name = "Braket AwsDevice for PennyLane"
    short_name = "braket.aws.qubit"

    def __init__(
        self,
        wires: Union[int, Iterable],
        device_arn: str,
        s3_destination_folder: AwsSession.S3DestinationFolder = None,
        *,
        shots: Union[int, None, Shots] = Shots.DEFAULT,
        poll_timeout_seconds: float = AwsQuantumTask.DEFAULT_RESULTS_POLL_TIMEOUT,
        poll_interval_seconds: float = AwsQuantumTask.DEFAULT_RESULTS_POLL_INTERVAL,
        aws_session: Optional[AwsSession] = None,
        parallel: bool = False,
        max_parallel: Optional[int] = None,
        max_connections: int = AwsQuantumTaskBatch.MAX_CONNECTIONS_DEFAULT,
        max_retries: int = AwsQuantumTaskBatch.MAX_RETRIES,
        **run_kwargs,
    ):
        device = AwsDevice(device_arn, aws_session=aws_session)
        user_agent = f"BraketPennylanePlugin/{__version__}"
        device.aws_session.add_braket_user_agent(user_agent)
        if DeviceActionType.JAQCD not in device.properties.action:
            raise ValueError(f"Device {device.name} does not support quantum circuits")

        device_type = device.type
        if device_type not in (AwsDeviceType.SIMULATOR, AwsDeviceType.QPU):
            raise ValueError(f"Invalid device type: {device_type}")

        if shots == Shots.DEFAULT and device_type == AwsDeviceType.SIMULATOR:
            num_shots = AwsDevice.DEFAULT_SHOTS_SIMULATOR
        elif shots == Shots.DEFAULT and device_type == AwsDeviceType.QPU:
            num_shots = AwsDevice.DEFAULT_SHOTS_QPU
        elif (shots is None or shots == 0) and device_type == AwsDeviceType.QPU:
            raise ValueError("QPU devices do not support 0 shots")
        else:
            num_shots = shots

        super().__init__(wires, device, shots=num_shots, **run_kwargs)
        self._s3_folder = s3_destination_folder
        self._poll_timeout_seconds = poll_timeout_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._parallel = parallel
        self._max_parallel = max_parallel
        self._max_connections = max_connections
        self._max_retries = max_retries

    @property
    def parallel(self):
        """bool: True if gradient calculations are evaluated in parallel."""
        return self._parallel

    def batch_execute(self, circuits, **run_kwargs):
        if not self._parallel:
            return super().batch_execute(circuits)

        for circuit in circuits:
            self.check_validity(circuit.operations, circuit.observables)
        braket_circuits = [
            self._pl_to_braket_circuit(circuit, **run_kwargs) for circuit in circuits
        ]

        batch_shots = 0 if self.analytic else self.shots

        task_batch = self._device.run_batch(
            braket_circuits,
            s3_destination_folder=self._s3_folder,
            shots=batch_shots,
            max_parallel=self._max_parallel,
            max_connections=self._max_connections,
            poll_timeout_seconds=self._poll_timeout_seconds,
            poll_interval_seconds=self._poll_interval_seconds,
            **self._run_kwargs,
        )
        # Call results() to retrieve the Braket results in parallel.
        try:
            braket_results_batch = task_batch.results(
                fail_unsuccessful=True, max_retries=self._max_retries
            )

        # Update the tracker before raising an exception further if some circuits do not complete.
        finally:
            if self.tracker.active:
                for task in task_batch.tasks:
                    tracking_data = self._tracking_data(task)
                    self.tracker.update(**tracking_data)
                total_executions = len(task_batch.tasks) - len(task_batch.unsuccessful)
                total_shots = total_executions * batch_shots
                self.tracker.update(batches=1, executions=total_executions, shots=total_shots)
                self.tracker.record()

        return [
            self._braket_to_pl_result(braket_result, circuit)
            for braket_result, circuit in zip(braket_results_batch, circuits)
        ]

    def _run_task(self, circuit):
        return self._device.run(
            circuit,
            s3_destination_folder=self._s3_folder,
            shots=0 if self.analytic else self.shots,
            poll_timeout_seconds=self._poll_timeout_seconds,
            poll_interval_seconds=self._poll_interval_seconds,
            **self._run_kwargs,
        )


class BraketLocalQubitDevice(BraketQubitDevice):
    r"""Amazon Braket LocalSimulator qubit device for PennyLane.

    Args:
        wires (int or Iterable[Number, str]]): Number of subsystems represented by the device,
            or iterable that contains unique labels for the subsystems as numbers
            (i.e., ``[-1, 0, 2]``) or strings (``['ancilla', 'q1', 'q2']``).
        backend (Union[str, BraketSimulator]): The name of the simulator backend or
            the actual simulator instance to use for simulation. Defaults to the
            ``default`` simulator backend name.
        shots (int or None): Number of circuit evaluations or random samples included,
            to estimate expectation values of observables. If this value is set to ``None`` or
            ``0``, then the device runs in analytic mode (calculations will be exact).
            Default: None
        **run_kwargs: Variable length keyword arguments for ``braket.devices.Device.run()``.
    """
    name = "Braket LocalSimulator for PennyLane"
    short_name = "braket.local.qubit"

    def __init__(
        self,
        wires: Union[int, Iterable],
        backend: Union[str, BraketSimulator] = "default",
        *,
        shots: Union[int, None] = None,
        **run_kwargs,
    ):
        device = LocalSimulator(backend)
        super().__init__(wires, device, shots=shots, **run_kwargs)

    def _run_task(self, circuit):
        return self._device.run(
            circuit, shots=0 if self.analytic else self.shots, **self._run_kwargs
        )
