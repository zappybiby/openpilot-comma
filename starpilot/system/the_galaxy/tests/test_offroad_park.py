import ast
import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from cereal import car, custom
from opendbc.can.parser import CANParser
from opendbc.can.packer import CANPacker
from opendbc.car import Bus
from opendbc.car.subaru.values import CAR, DBC


@pytest.fixture
def reader():
  source = Path(__file__).parents[1] / "the_galaxy.py"
  tree = ast.parse(source.read_text())
  helpers = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in {
    "_get_vehicle_parked", "_get_offroad_vehicle_parked",
  }]
  cp = car.CarParams.new_message(brand="subaru", carFingerprint=CAR.SUBARU_IMPREZA_2020,
                                 flags=int(CAR.SUBARU_IMPREZA_2020.config.flags))
  values = {"CarParamsPersistent": cp.to_bytes(), "StarPilotCarParamsPersistent": custom.StarPilotCarParams.new_message().to_bytes()}
  clock = SimpleNamespace(now=1_000_000_000)
  batches = []

  def receive(sock, wait_for_one):
    assert sock == "can" and wait_for_one
    clock.now += 100_000_000
    return batches.pop(0) if batches else []

  def subscribe(service, timeout):
    assert service == "can" and timeout == 100
    return service

  namespace = {
    "car": car, "custom": custom, "CANParser": CANParser, "importlib": importlib,
    "time": SimpleNamespace(monotonic_ns=lambda: clock.now),
    "messaging": SimpleNamespace(sub_sock=subscribe, drain_sock=receive, SubMaster=lambda *args, **kwargs: SimpleNamespace(
      update=lambda timeout: None, seen={"carState": False}, alive={"carState": False}, valid={"carState": False},
    )),
    "params": SimpleNamespace(get_bool=lambda key: False),
    "_safe_params_get_live_raw": values.get,
  }
  exec(compile(ast.Module(body=helpers, type_ignores=[]), str(source), "exec"), namespace)
  packer = CANPacker(DBC[CAR.SUBARU_IMPREZA_2020][Bus.pt])

  def packet(gear, *, bus=0, timestamp=1_100_000_000, valid=True, corrupt=False, short=False):
    address, dat, src = packer.make_can_msg("Transmission", bus, {"Gear": gear})
    if corrupt:
      dat = bytes([dat[0] ^ 0xFF]) + dat[1:]
    if short:
      dat = dat[:1]
    return SimpleNamespace(logMonoTime=timestamp, valid=valid, can=[SimpleNamespace(address=address, dat=dat, src=src)])

  return SimpleNamespace(read=namespace["_get_vehicle_parked"], packet=packet, batches=batches, clock=clock, values=values)


@pytest.mark.parametrize("gear,expected", [(4, True), (2, False), (3, False), (121, False), (0, False)])
def test_fresh_gear(reader, gear, expected):
  reader.batches.append([reader.packet(gear)])
  assert reader.read() is expected


@pytest.mark.parametrize("options", [
  {"bus": 1}, {"valid": False}, {"corrupt": True}, {"short": True},
  {"timestamp": 900_000_000}, {"timestamp": 1_200_000_000},
])
def test_unusable_park_packet_is_rejected(reader, options):
  reader.batches.append([reader.packet(4, **options)])
  assert reader.read() is False


def test_no_can_times_out(reader):
  assert reader.read() is False
  assert reader.clock.now <= 2_000_000_000


def test_park_is_not_reused_between_requests(reader):
  reader.batches.append([reader.packet(4)])
  assert reader.read() is True
  assert reader.read() is False


@pytest.mark.parametrize("gears,expected", [([4, 121], False), ([121, 4], True)])
def test_latest_gear_in_batch_wins(reader, gears, expected):
  reader.batches.append([reader.packet(g) for g in gears])
  assert reader.read() is expected


def test_old_packet_received_during_request_is_rejected(reader):
  reader.batches.extend([[], [], [reader.packet(4, timestamp=1_050_000_000)]])
  assert reader.read() is False


@pytest.mark.parametrize("key", ["CarParamsPersistent", "StarPilotCarParamsPersistent"])
def test_missing_configuration_is_rejected(reader, key):
  del reader.values[key]
  assert reader.read() is False


def test_corrupt_configuration_is_rejected(reader):
  reader.values["CarParamsPersistent"] = b"invalid"
  assert reader.read() is False


def test_unknown_vehicle_is_rejected(reader):
  reader.values["CarParamsPersistent"] = car.CarParams.new_message(brand="unsupported").to_bytes()
  assert reader.read() is False
