"""Bounded independent Godot replay verification; never loads a client script."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import tempfile
import subprocess

from economy import EconomyError

MAX_FRAMES = 54000  # Fifteen minutes at the canonical 60 Hz caller cadence.
CHUNK_FRAMES = 120
MIN_FRAMES = 1


def canonical_frame(frame):
    if not isinstance(frame, list) or len(frame) != 6 or any(type(value) is not int for value in frame):
        raise EconomyError("invalid_replay", "Replay frames must contain six integers")
    if not all(6000 <= frame[index] <= 354000 for index in (0, 1)) or not all(-1000 <= frame[index] <= 1000 for index in (2, 3)) or not all(frame[index] in (0, 1) for index in (4, 5)):
        raise EconomyError("invalid_replay", "Replay inputs are outside the legal range")
    return json.dumps(frame, separators=(",", ":")).encode() + b"\n"


@dataclass
class ReplayUpload:
    players: tuple[str, str]
    seed: int
    created: float
    frames: list[list[list[int]]] = field(default_factory=lambda: [[], []])
    hashes: list = field(default_factory=lambda: [hashlib.sha256(), hashlib.sha256()])
    seals: list[str | None] = field(default_factory=lambda: [None, None])
    verifying: bool = False
    last_frame_at: list[float] = field(default_factory=lambda: [0.0, 0.0])
    presence_at: list[float] = field(default_factory=lambda: [0.0, 0.0])
    mode: str = "casual"
    conceded: int | None = None

    def append(self, account_id, offset, frames):
        if account_id not in self.players:
            raise EconomyError("wrong_match", "This account is not a player in this match")
        index = self.players.index(account_id)
        if type(offset) is not int or offset < 0:
            raise EconomyError("replay_offset", "Replay chunks must arrive in order")
        if not isinstance(frames, list) or not 1 <= len(frames) <= CHUNK_FRAMES or offset + len(frames) > MAX_FRAMES:
            raise EconomyError("replay_size", "Replay chunk or match exceeds its frame limit")
        # Validate the entire chunk before mutating the hash or accumulated frames.
        encoded = [canonical_frame(frame) for frame in frames]
        # Acknowledgments can be lost during a signaling reconnect. Only an exact,
        # wholly contained retry is idempotent; partial overlaps remain invalid.
        end = offset + len(frames)
        if end <= len(self.frames[index]) and self.frames[index][offset:end] == frames:
            return len(self.frames[index])
        if self.seals[index] or self.verifying:
            raise EconomyError("replay_sealed", "This replay has already been sealed")
        if offset != len(self.frames[index]):
            raise EconomyError("replay_offset", "Replay chunks must arrive in order without gaps or changed overlap")
        for line in encoded:
            self.hashes[index].update(line)
        self.frames[index].extend(frames)
        return len(self.frames[index])

    def seal(self, account_id, digest, frames):
        if account_id not in self.players:
            raise EconomyError("wrong_match", "This account is not a player in this match")
        index = self.players.index(account_id)
        if type(frames) is not int or frames != len(self.frames[index]) or not MIN_FRAMES <= frames <= MAX_FRAMES or digest != self.hashes[index].hexdigest():
            raise EconomyError("replay_digest", "Replay count or digest does not match the uploaded input frames")
        self.seals[index] = digest
        if not all(self.seals):
            return False
        if self.seals[0] != self.seals[1] or len(self.frames[0]) != len(self.frames[1]):
            raise EconomyError("replay_disputed", "The two players did not corroborate the same applied inputs")
        self.verifying = True
        return True


class GodotReplayVerifier:
    def __init__(self, executable, simulation_path, *, timeout=45):
        self.executable = Path(executable).resolve()
        self.simulation = Path(simulation_path).resolve()
        self.project = Path(__file__).resolve().parent / "verifier"
        if not self.executable.is_file() or not self.simulation.is_file():
            raise ValueError("Replay verification requires a real Godot executable and the shipped simulation source")
        version = subprocess.run([str(self.executable), "--headless", "--version"], capture_output=True, text=True, timeout=10,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if version.returncode != 0 or not version.stdout.strip().startswith("4.7.2.stable"):
            raise ValueError("Replay verifier must use the same verified Godot 4.7.2 stable release as the clients")
        self.simulation_hash = hashlib.sha256(self.simulation.read_bytes()).hexdigest()
        self.timeout = timeout
        self._slots = asyncio.Semaphore(2)

    @classmethod
    def from_environment(cls):
        executable = os.environ.get("PB_GODOT_BINARY", "")
        if not executable:
            return None
        packaged = Path(__file__).resolve().parent / "verifier" / "simulation.gd"
        simulation = os.environ.get("PB_REPLAY_SIMULATION", str(packaged if packaged.exists() else Path(__file__).resolve().parents[1] / "scripts" / "simulation.gd"))
        return cls(executable, simulation)

    async def verify(self, seed, frames, *, allow_incomplete=False):
        if hashlib.sha256(self.simulation.read_bytes()).hexdigest() != self.simulation_hash:
            raise EconomyError("verifier_changed", "Simulation source changed; restart the verifier before awarding matches")
        if not MIN_FRAMES <= len(frames) <= MAX_FRAMES:
            raise EconomyError("replay_size", "The replay is outside the rewarded match duration")
        async with self._slots:
            with tempfile.TemporaryDirectory(prefix="pong-verified-replay-") as directory:
                path = Path(directory) / "replay.json"
                path.write_text(json.dumps({"version": 1, "seed": seed, "frames": frames, "allow_incomplete": allow_incomplete}, separators=(",", ":")), encoding="utf-8")
                process = await asyncio.create_subprocess_exec(
                    str(self.executable), "--headless", "--path", str(self.project), "--script", "res://replay.gd", "--",
                    "--replay", str(path), "--simulation", str(self.simulation),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                try:
                    output, _errors = await asyncio.wait_for(process.communicate(), timeout=self.timeout)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    process.kill()
                    await process.communicate()
                    raise EconomyError("verifier_timeout", "Replay verification timed out; this match was not rewarded") from None
                if process.returncode != 0 or len(output) > 65536:
                    raise EconomyError("verifier_failed", "The independent replay verifier could not validate this match")
                records = [line[len(b"PB_VERIFIED:"):] for line in output.splitlines() if line.startswith(b"PB_VERIFIED:")]
                try:
                    result = json.loads(records[-1]) if len(records) == 1 else None
                except ValueError:
                    result = None
                if not isinstance(result, dict) or result.get("winner") not in (-1, 0, 1) or result.get("frames") != len(frames) or result.get("phase") not in (("playing", "finished") if allow_incomplete else ("finished",)):
                    raise EconomyError("replay_incomplete", "The recorded inputs did not produce a completed match")
                return result
