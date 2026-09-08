"""API + real Subaru decoding; simulated clock/transport/device params.

Run this file separately from other Galaxy tests with RECORDING_PATH pointing
at the decoded uploaded rlog. Set PARK_IMPLEMENTATION=baseline to substitute
the original Dom Park check. TARGET_REPO optionally selects a checkout.
"""
import ast
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from cereal import car, custom, log
from opendbc.can.parser import CANParser
from opendbc.can.packer import CANPacker
from opendbc.car import Bus
from opendbc.car.carlog import carlog
from opendbc.car.subaru.carstate import CarState
from opendbc.car.subaru.values import DBC

ROOT = Path(os.environ.get("TARGET_REPO", Path(__file__).resolve().parents[4]))
BASE = "2504441a4e18f8f825e51eced00c32acfa990cff"
sys.path.insert(0, str(ROOT / "starpilot/system/the_galaxy/tests"))
real_modules = {k: v for k, v in sys.modules.items() if k == "cereal" or k.startswith(("cereal.", "opendbc."))}
import test_navigation_params as api
sys.modules.update(real_modules)
carlog.disabled = True
assert CarState.__module__ == "opendbc.car.subaru.carstate"


@pytest.fixture(scope="module")
def recording():
  packets = []
  with open(Path(os.environ["RECORDING_PATH"]), "rb") as stream:
    events = log.Event.read_multiple(stream)
    init = next(events)
    values = {p.key: bytes(p.value) for p in init.initData.params.entries
              if p.key in ("CarParamsPersistent", "StarPilotCarParamsPersistent")}
    first = None
    for event in events:
      if event.which() != "can":
        continue
      first = event.logMonoTime if first is None else first
      if event.logMonoTime < first + 2_000_000_000:
        continue
      if packets and event.logMonoTime > packets[0][0] + 3_200_000_000:
        break
      packets.append((event.logMonoTime, [(c.address, bytes(c.dat), c.src) for c in event.can]))
  with car.CarParams.from_bytes(values["CarParamsPersistent"]) as cp:
    assert cp.brand == "subaru" and cp.carFingerprint == "SUBARU_IMPREZA_2020"
    dbc = DBC[cp.carFingerprint][Bus.pt]
  return values, packets, dbc


@pytest.fixture
def rig(monkeypatch, recording):
  values, packets, dbc = recording
  values = dict(values)
  client, params = api._params_client(monkeypatch, {"IsOnroad": False, "ForceOffroad": False, "ForceOnroad": False}, "mici")
  clock = SimpleNamespace(now=10_000_000_000, boot_offset=0)
  delivery = []
  live = SimpleNamespace(gear="park", seen=True, alive=True, valid=True)
  sockets = []

  def subscribe(service, timeout):
    assert service == "can" and timeout == 100
    sockets.append(service)
    return service

  def receive(sock, wait_for_one):
    assert sock == "can" and wait_for_one
    if delivery:
      clock.now, packet = delivery.pop(0)
      return [packet]
    clock.now += 100_000_000
    return []

  class SubMaster(dict):
    def __init__(self, *args, **kwargs):
      super().__init__(carState=SimpleNamespace(gearShifter=getattr(car.CarState.GearShifter, live.gear)))
      self.seen = {"carState": live.seen}
      self.alive = {"carState": live.alive}
      self.valid = {"carState": live.valid}

    def update(self, timeout):
      assert timeout == 100

  for key, value in {
    "car": car, "custom": custom, "CANParser": CANParser,
    "time": SimpleNamespace(monotonic_ns=lambda: clock.now, CLOCK_BOOTTIME=7, CLOCK_MONOTONIC=1,
                            clock_gettime_ns=lambda clock_id: clock.now + (clock.boot_offset if clock_id == 7 else 0)),
    "messaging": SimpleNamespace(sub_sock=subscribe, drain_sock=receive, SubMaster=SubMaster),
    "_safe_params_get_live_raw": values.get,
  }.items():
    monkeypatch.setattr(api.the_galaxy, key, value)

  if os.environ.get("PARK_IMPLEMENTATION") == "baseline":
    source = subprocess.check_output(["git", "show", BASE + ":starpilot/system/the_galaxy/the_galaxy.py"], cwd=ROOT, text=True)
    node = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == "_get_vehicle_parked")
    scope = dict(api.the_galaxy.__dict__)
    exec(compile(ast.Module(body=[node], type_ignores=[]), "original_dom_park_check", "exec"), scope)
    monkeypatch.setattr(api.the_galaxy, "_get_vehicle_parked", scope["_get_vehicle_parked"])

  def stream(gear=None, *, invalid=False, old=False, corrupt_gear=False):
    delivery.clear()
    live.seen = live.alive = live.valid = bool(params.values["IsOnroad"])
    decoder = CANParser(dbc, [("Transmission", 100)], 0)
    packer = CANPacker(dbc)
    address = decoder.dbc.name_to_msg["Transmission"].address
    start = clock.now
    for stamp, frames in packets:
      offset = stamp - packets[0][0] + 1
      updated = []
      for addr, data, bus in frames:
        if addr == address and bus == 0:
          if gear is not None:
            decoder.update([(stamp, [(addr, data, bus)])])
            fields = dict(decoder.vl["Transmission"])
            fields["Gear"] = gear(offset) if callable(gear) else gear
            addr, data, bus = packer.make_can_msg("Transmission", bus, fields)
          if corrupt_gear:
            data = bytes([data[0] ^ 0xFF]) + data[1:]
        updated.append(SimpleNamespace(address=addr, dat=data, src=bus))
      packet = SimpleNamespace(logMonoTime=(start - 4_000_000_000 if old else start) + offset + clock.boot_offset,
                               valid=not invalid, can=updated)
      delivery.append((start + offset, packet))

  def request(value, expected):
    before = dict(params.values)
    response = client.put("/api/params", json={"key": "ForceOffroad", "value": value})
    assert response.status_code == expected, response.get_json()
    if expected == 200:
      assert params.values["ForceOffroad"] is value
      assert params.values["ForceOnroad"] is False
    else:
      assert params.values == before
    return response

  return SimpleNamespace(client=client, stream=stream, request=request, read=api.the_galaxy._get_vehicle_parked,
                         params=params, live=live, clock=clock, delivery=delivery, values=values, sockets=sockets)


def test_drive_to_park_onroad(rig):
  rig.params.values["IsOnroad"] = True
  rig.live.gear = "drive"
  rig.request(True, 403)
  rig.live.gear = "park"
  rig.request(True, 200)
  assert not rig.sockets


def test_galaxy_enable_then_disable_after_card_stops(rig, record_property):
  rig.params.values["IsOnroad"] = True
  rig.request(True, 200)
  rig.params.values["IsOnroad"] = False
  rig.stream()
  before = rig.clock.now
  rig.request(False, 200)
  assert rig.clock.now - before < 3_000_000_000
  record_property("simulated_check_ms", round((rig.clock.now - before) / 1e6, 1))


def test_device_selects_offroad_then_galaxy_disables(rig):
  rig.params.values.update(ForceOffroad=True, ForceOnroad=False, IsOnroad=False)
  rig.stream()
  rig.request(False, 200)


def test_device_selects_auto_then_galaxy_enables_before_card_starts(rig):
  rig.params.values.update(ForceOffroad=False, ForceOnroad=False, IsOnroad=False)
  rig.stream()
  rig.request(True, 200)


def test_device_selects_onroad_then_galaxy_restores_auto(rig):
  rig.params.values.update(ForceOffroad=False, ForceOnroad=True, IsOnroad=True)
  rig.request(False, 200)


def test_onroad_startup_waits_for_valid_car_state(rig):
  rig.params.values.update(ForceOffroad=False, ForceOnroad=False, IsOnroad=True)
  rig.live.seen = rig.live.alive = rig.live.valid = False
  rig.request(True, 403)
  rig.request(False, 403)
  rig.live.seen = rig.live.alive = rig.live.valid = True
  rig.request(True, 200)


def test_leave_park_while_offroad_then_return(rig):
  rig.params.values["ForceOffroad"] = True
  for value in (False, True):
    rig.stream(121)
    rig.request(value, 403)
  rig.stream(4)
  rig.request(False, 200)


@pytest.mark.parametrize("gear", [2, 3, 0], ids=["neutral", "reverse", "unknown"])
def test_other_gears_reject_both_directions(rig, gear):
  for value in (True, False):
    rig.stream(gear)
    rig.request(value, 403)


def test_each_request_needs_new_can(rig):
  rig.stream()
  rig.request(True, 200)
  rig.delivery.clear()
  rig.request(False, 403)
  rig.stream()
  rig.request(False, 200)


@pytest.mark.parametrize("first,last,expected", [(4, 121, 403), (121, 4, 200)], ids=["park-to-drive", "drive-to-park"])
def test_gear_changes_during_request(rig, first, last, expected):
  rig.stream(lambda offset: first if offset < 800_000_000 else last)
  rig.request(True, expected)


@pytest.mark.parametrize("options", [{"invalid": True}, {"old": True}, {"corrupt_gear": True}],
                         ids=["invalid-packets", "pre-request-packets", "bad-gear-checksum"])
def test_unusable_can_does_not_prove_park(rig, options):
  for value in (True, False):
    rig.stream(**options)
    rig.request(value, 403)


def test_missing_configuration_and_no_can_reject(rig):
  rig.live.seen = rig.live.alive = rig.live.valid = False
  for value in (True, False):
    rig.request(value, 403)
  del rig.values["CarParamsPersistent"]
  rig.stream()
  rig.request(False, 403)


def test_settings_park_status_remains_available_after_offroad(rig):
  rig.params.values["IsOnroad"] = True
  response = rig.client.get("/api/params/all")
  assert response.status_code == 200 and response.get_json()["VehicleParked"] is True
  rig.request(True, 200)
  rig.params.values["IsOnroad"] = False
  rig.stream(4)
  response = rig.client.get("/api/params/all")
  assert response.status_code == 200 and response.get_json()["VehicleParked"] is True
  rig.stream(121)
  response = rig.client.get("/api/params/all")
  assert response.status_code == 200 and response.get_json()["VehicleParked"] is False


def test_can_timestamps_use_publisher_clock(rig):
  rig.clock.boot_offset = 60_000_000_000
  for value in (True, False):
    rig.stream()
    rig.request(value, 200)
