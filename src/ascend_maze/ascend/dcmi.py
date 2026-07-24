"""Small public-API-only ctypes wrapper around libdcmi."""

from __future__ import annotations

import ctypes
from pathlib import Path
from threading import RLock
import time

from ascend_maze.ascend.contracts import AscendDeviceSnapshot, AscendProcessSnapshot

_MAX_CARDS = 64
_MAX_PROCESSES = 1_024
_MIB = 1024 * 1024


class DcmiError(RuntimeError):
    def __init__(self, operation: str, return_code: int) -> None:
        super().__init__(f"{operation} failed with DCMI return code {return_code}")
        self.operation = operation
        self.return_code = return_code


class _ChipInfo(ctypes.Structure):
    _fields_ = [
        ("chip_type", ctypes.c_ubyte * 32),
        ("chip_name", ctypes.c_ubyte * 32),
        ("chip_ver", ctypes.c_ubyte * 32),
        ("aicore_cnt", ctypes.c_uint),
    ]


class _HbmInfo(ctypes.Structure):
    _fields_ = [
        ("memory_size", ctypes.c_ulonglong),
        ("freq", ctypes.c_uint),
        ("memory_usage", ctypes.c_ulonglong),
        ("temp", ctypes.c_int),
        ("bandwidth_util_rate", ctypes.c_uint),
    ]


class _ProcessMemory(ctypes.Structure):
    _fields_ = [
        ("proc_id", ctypes.c_int),
        ("proc_mem_usage", ctypes.c_ulong),
    ]


def _text(value: ctypes.Array[ctypes.c_ubyte]) -> str:
    return bytes(value).split(b"\0", 1)[0].decode("ascii", errors="replace")


class DcmiDeviceAdapter:
    """Discover physical NPUs and map worker PIDs without shelling out."""

    def __init__(self, library_path: str | Path = "libdcmi.so") -> None:
        self.library_path = str(library_path)
        self._lib = ctypes.CDLL(self.library_path)
        self._lock = RLock()
        self._configure_signatures()
        self._check("dcmi_init", self._lib.dcmi_init())

    def devices(self) -> tuple[AscendDeviceSnapshot, ...]:
        with self._lock:
            card_count = ctypes.c_int()
            cards = (ctypes.c_int * _MAX_CARDS)()
            self._check(
                "dcmi_get_card_list",
                self._lib.dcmi_get_card_list(
                    ctypes.byref(card_count), cards, _MAX_CARDS
                ),
            )
            result: list[AscendDeviceSnapshot] = []
            for card_id in sorted(cards[: card_count.value]):
                device_count = ctypes.c_int()
                self._check(
                    "dcmi_get_device_num_in_card",
                    self._lib.dcmi_get_device_num_in_card(
                        card_id, ctypes.byref(device_count)
                    ),
                )
                for card_device_id in range(device_count.value):
                    result.append(self._device(card_id, card_device_id))
            return tuple(sorted(result, key=lambda item: int(item.physical_device_id)))

    def device(self, physical_device_id: str) -> AscendDeviceSnapshot:
        for item in self.devices():
            if item.physical_device_id == physical_device_id:
                return item
        raise KeyError(f"unknown physical Ascend device: {physical_device_id}")

    def process_hbm_mb(self, physical_device_id: str, pid: int) -> int | None:
        return next(
            (
                process.hbm_mb
                for process in self.device(physical_device_id).processes
                if process.pid == pid
            ),
            None,
        )

    def verify_process_device(
        self,
        pid: int,
        physical_device_id: str,
        *,
        deadline_seconds: float = 2.0,
        poll_interval_seconds: float = 0.05,
    ) -> bool:
        deadline = time.monotonic() + deadline_seconds
        while True:
            if self.process_hbm_mb(physical_device_id, pid) is not None:
                return not any(
                    process.pid == pid
                    for device in self.devices()
                    if device.physical_device_id != physical_device_id
                    for process in device.processes
                )
            if time.monotonic() >= deadline:
                return False
            time.sleep(poll_interval_seconds)

    def _device(self, card_id: int, card_device_id: int) -> AscendDeviceSnapshot:
        logical_id = ctypes.c_int()
        self._check(
            "dcmi_get_device_logic_id",
            self._lib.dcmi_get_device_logic_id(
                ctypes.byref(logical_id), card_id, card_device_id
            ),
        )
        physical_id = ctypes.c_uint()
        self._check(
            "dcmi_get_device_phyid_from_logicid",
            self._lib.dcmi_get_device_phyid_from_logicid(
                logical_id.value, ctypes.byref(physical_id)
            ),
        )
        chip = _ChipInfo()
        self._check(
            "dcmi_get_device_chip_info",
            self._lib.dcmi_get_device_chip_info(
                card_id, card_device_id, ctypes.byref(chip)
            ),
        )
        hbm = _HbmInfo()
        self._check(
            "dcmi_get_device_hbm_info",
            self._lib.dcmi_get_device_hbm_info(
                card_id, card_device_id, ctypes.byref(hbm)
            ),
        )
        health = ctypes.c_uint()
        self._check(
            "dcmi_get_device_health",
            self._lib.dcmi_get_device_health(
                card_id, card_device_id, ctypes.byref(health)
            ),
        )
        utilization = ctypes.c_uint()
        utilization_result = self._lib.dcmi_get_device_utilization_rate(
            card_id, card_device_id, 13, ctypes.byref(utilization)
        )
        process_buffer = (_ProcessMemory * _MAX_PROCESSES)()
        process_count = ctypes.c_int(_MAX_PROCESSES)
        self._check(
            "dcmi_get_device_resource_info",
            self._lib.dcmi_get_device_resource_info(
                card_id,
                card_device_id,
                process_buffer,
                ctypes.byref(process_count),
            ),
        )
        processes = tuple(
            sorted(
                (
                    AscendProcessSnapshot(
                        item.proc_id,
                        (int(item.proc_mem_usage) + _MIB - 1) // _MIB,
                    )
                    for item in process_buffer[: process_count.value]
                    if item.proc_id > 0
                ),
                key=lambda item: item.pid,
            )
        )
        chip_name = _text(chip.chip_name)
        return AscendDeviceSnapshot(
            physical_device_id=str(physical_id.value),
            card_id=card_id,
            card_device_id=card_device_id,
            chip_type=chip_name or _text(chip.chip_type),
            chip_version=_text(chip.chip_ver) or "unknown",
            total_hbm_mb=int(hbm.memory_size),
            used_hbm_mb=int(hbm.memory_usage),
            health="healthy" if health.value == 0 else "unhealthy",
            utilization=(
                float(utilization.value) if utilization_result == 0 else None
            ),
            processes=processes,
        )

    def _configure_signatures(self) -> None:
        lib = self._lib
        lib.dcmi_init.restype = ctypes.c_int
        lib.dcmi_get_card_list.argtypes = [
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.c_int,
        ]
        lib.dcmi_get_device_num_in_card.argtypes = [
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int),
        ]
        lib.dcmi_get_device_logic_id.argtypes = [
            ctypes.POINTER(ctypes.c_int),
            ctypes.c_int,
            ctypes.c_int,
        ]
        lib.dcmi_get_device_phyid_from_logicid.argtypes = [
            ctypes.c_uint,
            ctypes.POINTER(ctypes.c_uint),
        ]
        lib.dcmi_get_device_chip_info.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(_ChipInfo),
        ]
        lib.dcmi_get_device_hbm_info.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(_HbmInfo),
        ]
        lib.dcmi_get_device_health.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_uint),
        ]
        lib.dcmi_get_device_utilization_rate.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_uint),
        ]
        lib.dcmi_get_device_resource_info.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(_ProcessMemory),
            ctypes.POINTER(ctypes.c_int),
        ]

    @staticmethod
    def _check(operation: str, return_code: int) -> None:
        if return_code != 0:
            raise DcmiError(operation, return_code)
