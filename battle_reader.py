"""Read the native battle statistics of one verified Windows client build."""

from __future__ import annotations

import hashlib
import math
import struct
from dataclasses import dataclass
from pathlib import Path

from win32_process import ProcessMemory

GAME_ASSEMBLY_SHA256 = "cf7956bf88a61986859ef55c026d231713e2d12cf69ffd9a19779ab491cf76ff"
METADATA_SHA256 = "e58fb854ae519bc22f7d8bc7ac83502425a51b896a907a3374f5215202f9caa5"

# These RVAs and object layouts were checked in the x64 client identified above.
_CONTROLLER_TYPE_INFO = 0x8B0FC40
_CONTROLLER_SINGLETON_METHOD = 0x8BB1B60
_AUTOCHESS_SINGLETON_METHOD = 0x8BD8990
_CLASS_STATIC_FIELDS = 0xB8
_AUTOCHESS_MODE = 15
_PHASES = {4: "普通", 5: "联防", 6: "领袖"}
_BATTLE_STATES = (2, 3)
_DAMAGE_TYPES = (1, 2, 3, 5)


class UnsupportedClient(RuntimeError):
    pass


class SnapshotError(RuntimeError):
    """A candidate snapshot could not be read consistently."""


@dataclass(frozen=True)
class Match:
    manager: int
    started_at: int


@dataclass(frozen=True)
class Battle:
    round_number: int
    phase: str


@dataclass(frozen=True)
class BattleSample:
    match: Match
    battle: Battle | None
    frame: int
    finished: bool
    match_ended: bool
    battle_started_at: int
    damage: dict[str, float]


@dataclass(frozen=True)
class _Context:
    match: Match
    round_number: int
    phase: int
    controller: int
    frame: int
    controller_state: int
    battle_started_at: int
    stats: int

    @property
    def battle(self) -> Battle | None:
        phase = _PHASES.get(self.phase)
        if phase is None or self.controller_state not in _BATTLE_STATES:
            return None
        return Battle(self.round_number, phase)


def check_client(module_path: Path) -> None:
    metadata_path = module_path.parent / "Arknights_Data" / "il2cpp_data" / "Metadata" / "global-metadata.dat"
    for path, expected in ((module_path, GAME_ASSEMBLY_SHA256), (metadata_path, METADATA_SHA256)):
        with path.open("rb") as file:
            actual = hashlib.file_digest(file, "sha256").hexdigest()
        if actual != expected:
            raise UnsupportedClient(f"此客户端版本尚未适配。{path.name} 的 SHA-256 为 {actual}。")


def _valid_pointer(value: int) -> bool:
    return 0x10000 <= value < (1 << 47) and value % 8 == 0


class BattleReader:
    def __init__(self, memory: ProcessMemory):
        self.memory = memory

    def _unpack(self, layout: str, address: int) -> tuple:
        return struct.unpack(layout, self.memory.read(address, struct.calcsize(layout)))

    def _pointer(self, address: int) -> int:
        value, = self._unpack("<Q", address)
        if value and not _valid_pointer(value):
            raise SnapshotError("客户端对象正在切换，指针暂不可读。")
        return value

    def _metadata_pointer(self, rva: int) -> int:
        address = self.memory.module_base + rva
        value, = self._unpack("<Q", address)
        # IL2CPP leaves an encoded metadata token here until its first use.
        if value & 1:
            return 0
        if value and not _valid_pointer(value):
            raise SnapshotError("客户端元数据指针无效。")
        return value

    def _singleton(self, rva: int, class_slot: int = 0) -> int:
        method = self._metadata_pointer(rva)
        if not method:
            return 0
        # The native getter obtains the generic singleton class from its RGCTX.
        declaring_class = self._pointer(method + 0x20)
        rgctx = self._pointer(declaring_class + 0xC0) if declaring_class else 0
        singleton_class = self._pointer(rgctx + class_slot) if rgctx else 0
        static_fields = self._pointer(singleton_class + _CLASS_STATIC_FIELDS) if singleton_class else 0
        return self._pointer(static_fields) if static_fields else 0

    def _string(self, address: int, max_length: int) -> str:
        if not _valid_pointer(address):
            raise SnapshotError("客户端字符串指针无效。")
        length, = self._unpack("<i", address + 0x10)
        if not 0 < length <= max_length:
            raise SnapshotError("客户端字符串长度无效。")
        value = self.memory.read(address + 0x14, 2 * length).decode("utf-16-le", errors="replace")
        if not value.isascii() or not value.isprintable():
            raise SnapshotError("客户端编号无效。")
        return value

    def _context(self) -> _Context | None:
        klass = self._metadata_pointer(_CONTROLLER_TYPE_INFO)
        fields = self._pointer(klass + _CLASS_STATIC_FIELDS) if klass else 0
        if not fields or self._unpack("<B", fields)[0] != _AUTOCHESS_MODE:
            return None

        manager = self._singleton(_AUTOCHESS_SINGLETON_METHOD, 0x18)
        controller = self._singleton(_CONTROLLER_SINGLETON_METHOD)
        if not manager or not controller:
            return None
        if self._pointer(controller) != klass:
            raise SnapshotError("战斗控制器类型不符。")

        status = self._pointer(manager + 0x38)
        center = self._pointer(manager + 0x40)
        state_model = self._pointer(status + 0x10) if status else 0
        state_machine = self._pointer(state_model + 0x18) if state_model else 0
        if not center or not state_machine:
            return None

        activity = self._pointer(center + 0x10)
        if not activity:
            return None
        if self._string(activity, 64) != "act3autochess":
            return None

        started_at, = self._unpack("<Q", manager + 0x10)
        round_number, = self._unpack("<i", center + 0x3C)
        phase, = self._unpack("<i", state_machine + 0x5C)
        frame, = self._unpack("<i", fields + 0x14)
        state, = self._unpack("<i", controller + 0x228)
        if not 0 <= round_number <= 128 or frame < 0 or state not in (0, 1, 2, 3):
            raise SnapshotError("回合、帧或战斗状态无效。")
        if phase in _PHASES and round_number < 1:
            raise SnapshotError("回合号尚未就绪。")
        battle_status = self._pointer(center + 0xF0) if phase in _PHASES else 0
        battle_started_at = self._unpack("<Q", battle_status + 0x28)[0] if battle_status else 0
        logger = self._pointer(controller + 0xE0)
        stats = self._pointer(logger + 0x30) if logger else 0
        return _Context(Match(manager, started_at), round_number, phase, controller, frame, state, battle_started_at, stats)

    def _list(self, address: int, item_size: int, limit: int) -> bytes:
        header_address = address + 0x10
        header = self.memory.read(header_address, 0x10)
        items, count, _ = struct.unpack("<Qii", header)
        if count < 0 or count > limit:
            raise SnapshotError("战斗统计列表长度无效。")
        content = b""
        if count:
            if not _valid_pointer(items):
                raise SnapshotError("战斗统计数组尚未就绪。")
            capacity, = self._unpack("<Q", items + 0x18)
            if not count <= capacity <= 4096:
                raise SnapshotError("战斗统计数组容量无效。")
            content = self.memory.read(items + 0x20, count * item_size)
        if header != self.memory.read(header_address, 0x10):
            raise SnapshotError("采样期间战斗统计列表已变化。")
        return content

    @staticmethod
    def _valid_damage(value: float) -> bool:
        return math.isfinite(value) and abs(value) <= 1e12

    def _damage(self, address: int) -> float:
        if not _valid_pointer(address):
            raise SnapshotError("干员统计对象无效。")
        raw_total, = self._unpack("<f", address + 0x20)
        if not self._valid_damage(raw_total):
            raise SnapshotError("干员伤害值无效。")
        by_type = self._pointer(address + 0x38)
        if by_type:
            data = self._list(by_type, 4, 32)
            buckets = tuple(value for value, in struct.iter_unpack("<f", data))
            if len(buckets) > max(_DAMAGE_TYPES):
                values = [buckets[index] for index in _DAMAGE_TYPES]
                if not all(self._valid_damage(value) and value >= 0 for value in values):
                    raise SnapshotError("干员伤害类型计数无效。")
                return sum(values)
        # On this client, the HP-removal aggregate uses a negative delta.
        return max(0.0, -raw_total)

    def sample(self) -> BattleSample | None:
        before = self._context()
        if before is None:
            return None
        battle = before.battle
        amounts: dict[str, float] = {}
        if battle is not None:
            if not before.stats:
                raise SnapshotError("战斗统计对象尚未就绪。")
            operators = self._pointer(before.stats + 0x30)
            if not operators:
                raise SnapshotError("干员统计列表尚未就绪。")
            entries = self._list(operators, 0x10, 256)
            for key, value in struct.iter_unpack("<QQ", entries):
                character_id = self._string(key, 192)
                if character_id in amounts:
                    raise SnapshotError("战斗统计中出现重复的干员编号。")
                amounts[character_id] = self._damage(value)
            if self._pointer(before.stats + 0x30) != operators or self._list(operators, 0x10, 256) != entries:
                raise SnapshotError("采样期间干员统计对象已变化。")
        if before != self._context():
            raise SnapshotError("采样跨越了战斗帧或回合。")
        return BattleSample(
            before.match, battle, before.frame, before.controller_state == 3,
            before.phase == 8, before.battle_started_at, amounts,
        )
