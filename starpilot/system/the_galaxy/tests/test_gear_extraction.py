"""Compare extracted gear logic with the unchanged Dom implementation."""
import ast
import importlib
import itertools
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from cereal import car
from opendbc.car import Bus

BASE = "2504441a4e18f8f825e51eced00c32acfa990cff"
ROOT = Path(__file__).resolve().parents[4]
BRANDS = ["chrysler", "ford", "gm", "honda", "hyundai", "mazda", "nissan", "rivian", "subaru", "tesla", "toyota", "volkswagen", "volvo"]


def original_gear_statements(statements):
  result = []
  intermediates = {"gear", "can_gear", "cp_transmission", "gear_position", "gearPosition", "reverse_light"}
  for node in statements:
    if isinstance(node, ast.Assign):
      if any(isinstance(t, ast.Attribute) and t.attr == "gearShifter" or isinstance(t, ast.Name) and t.id in intermediates for t in node.targets):
        result.append(node)
    elif isinstance(node, ast.If):
      body, other = original_gear_statements(node.body), original_gear_statements(node.orelse)
      if body or other:
        result.append(ast.If(test=node.test, body=body or [ast.Pass()], orelse=other))
  return result


class Signals:
  def __init__(self, accesses, bus, seed):
    self.accesses, self.bus, self.seed = accesses, bus, seed

  def __getitem__(self, message):
    owner = self

    class Message:
      def __getitem__(self, signal):
        owner.accesses.append((owner.bus, message, signal))
        return (owner.seed + sum(map(ord, message + signal))) % 16

    return Message()


@pytest.mark.parametrize("brand", BRANDS)
def test_gear_outputs_and_signal_selection_match_dom(brand):
  module = importlib.import_module(f"opendbc.car.{brand}.carstate")
  source = subprocess.check_output(["git", "show", f"{BASE}:opendbc_repo/opendbc/car/{brand}/carstate.py"], cwd=ROOT, text=True)
  cls = next(n for n in ast.parse(source).body if isinstance(n, ast.ClassDef) and n.name == "CarState")
  originals = {}
  for method in cls.body:
    if isinstance(method, ast.FunctionDef) and method.name.startswith("update"):
      body = original_gear_statements(method.body)
      if body:
        originals[method.name] = compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])), "Dom gear logic", "exec")

  flag_names = {"ford": "FordFlags", "hyundai": "HyundaiFlags", "subaru": "SubaruFlags", "toyota": "ToyotaFlags", "volkswagen": "VolkswagenFlags"}
  flag_class = getattr(module, flag_names.get(brand, ""), None)
  names = {
    "ford": ["CANFD", "ALT_STEER_ANGLE"],
    "hyundai": ["CANFD", "HYBRID", "EV", "FCEV", "CLUSTER_GEARS", "TCU_GEARS"],
    "subaru": ["HYBRID"], "toyota": ["SECOC"],
    "volkswagen": ["PQ", "MLB", "MEB", "ALT_GEAR"],
  }.get(brand, [])
  flags = [sum(int(getattr(flag_class, n)) for n, selected in zip(names, mask, strict=True) if selected)
           for mask in itertools.product([False, True], repeat=len(names))]
  candidates = ["other"] + ([next(iter(module.RAM_CARS))] if brand == "chrysler" else [])
  transmissions = [getattr(car.CarParams.TransmissionType, x) for x in ["unknown", "automatic", "manual", "direct"]]
  shifters = dict(enumerate(["P", "R", "N", "D", "S", "L", "T", None] * 2))
  for flag, transmission, candidate, seed in itertools.product(flags, transmissions, candidates, range(16)):
    instance = module.CarState.__new__(module.CarState)
    instance.CP = SimpleNamespace(flags=flag, transmissionType=transmission, carFingerprint=candidate)
    instance.shifter_values = shifters
    instance.CCP = SimpleNamespace(shifter_values=shifters)
    instance.car_state_scm_msg, instance.gearbox_msg, instance.gear_msg_canfd = "SCM", "GEARBOX", "GEAR_ALT"
    instance.can_define = SimpleNamespace(dv={"DI_systemStatus": {"DI_gear": dict(enumerate(list(getattr(module, "GEAR_MAP", {})) * 4))}})
    accesses = []
    parsers = {bus: SimpleNamespace(vl=Signals(accesses, bus, seed)) for bus in Bus}
    method = "update"
    if brand == "hyundai" and flag & module.HyundaiFlags.CANFD:
      method = "update_canfd"
    if brand == "volkswagen":
      for name in ["PQ", "MLB", "MEB"]:
        if flag & getattr(module.VolkswagenFlags, name):
          method = "update_" + name.lower()
          break
    ret = car.CarState.new_message()
    scope = dict(vars(module), self=instance, ret=ret, cp=parsers[Bus.pt], pt_cp=parsers[Bus.pt],
                 cp_alt=parsers[Bus.alt], cp_main=parsers[Bus.main], cp_party=parsers[Bus.party])
    exec(originals[method], scope)
    expected_accesses = accesses.copy()
    accesses.clear()
    actual = instance.get_gear_shifter(parsers)
    assert actual == ret.gearShifter, (brand, flag, transmission, candidate, seed)
    assert accesses == expected_accesses, (brand, flag, transmission, candidate, seed)
