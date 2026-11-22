"""Execute gate-health.lua with local Lua libraries, not Argo's embedded VM.

Uses only Python's standard library. Install a Lua 5.2 or 5.4 shared library to
run these tests; this checks the actual script's results for Kubernetes-shaped
objects, but live Argo health evaluation remains a separate acceptance check.
"""
import copy
import re
import ctypes
import ctypes.util
import os
from pathlib import Path
import unittest


SCRIPT = Path(os.environ.get(
    "PREVIEW_GATE_HEALTH_SCRIPT",
    str(Path(__file__).resolve().parents[1] / "gate-health.lua"),
))
PROJECT = re.search(r'labels\["company.example/source-project-id"\] ~= "([1-9][0-9]*)"', SCRIPT.read_text())[1]
OTHER_PROJECT = str(int(PROJECT) + 1)

LIBRARIES = [(version, name) for version in ("5.2", "5.4")
             if (name := ctypes.util.find_library("lua" + version))]


class LuaRuntime:
    """A small C-API bridge with native value conversion and protected calls."""

    def __init__(self, version, library):
        self.lib = ctypes.CDLL(library)
        state = ctypes.c_void_p
        integer = ctypes.c_ssize_t if version == "5.2" else ctypes.c_longlong
        context = ctypes.c_int if version == "5.2" else ctypes.c_ssize_t
        signatures = {
            "luaL_newstate": ([], state),
            "luaL_openlibs": ([state], None),
            "lua_close": ([state], None),
            "lua_createtable": ([state, ctypes.c_int, ctypes.c_int], None),
            "lua_pushnil": ([state], None),
            "lua_pushboolean": ([state, ctypes.c_int], None),
            "lua_pushinteger": ([state, integer], None),
            "lua_pushnumber": ([state, ctypes.c_double], None),
            "lua_pushlstring": ([state, ctypes.c_char_p, ctypes.c_size_t], None),
            "lua_setfield": ([state, ctypes.c_int, ctypes.c_char_p], None),
            "lua_rawseti": ([state, ctypes.c_int,
                             ctypes.c_int if version == "5.2" else integer], None),
            "lua_setglobal": ([state, ctypes.c_char_p], None),
            "lua_getfield": ([state, ctypes.c_int, ctypes.c_char_p], None),
            "lua_settop": ([state, ctypes.c_int], None),
            "lua_type": ([state, ctypes.c_int], ctypes.c_int),
            "lua_tolstring": ([state, ctypes.c_int,
                               ctypes.POINTER(ctypes.c_size_t)], ctypes.c_void_p),
            "luaL_loadbufferx": ([state, ctypes.c_char_p, ctypes.c_size_t,
                                  ctypes.c_char_p, ctypes.c_char_p], ctypes.c_int),
            "lua_pcallk": ([state, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                            context, ctypes.c_void_p], ctypes.c_int),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.lib, name)
            function.argtypes = arguments
            function.restype = result

    def push(self, state, value):
        if value is None:
            self.lib.lua_pushnil(state)
        elif isinstance(value, bool):
            self.lib.lua_pushboolean(state, int(value))
        elif isinstance(value, int):
            self.lib.lua_pushinteger(state, value)
        elif isinstance(value, float):
            self.lib.lua_pushnumber(state, value)
        elif isinstance(value, str):
            encoded = value.encode()
            self.lib.lua_pushlstring(state, encoded, len(encoded))
        elif isinstance(value, dict):
            self.lib.lua_createtable(state, 0, len(value))
            for key, item in value.items():
                if not isinstance(key, str):
                    raise TypeError("Lua object keys must be strings")
                self.push(state, item)
                self.lib.lua_setfield(state, -2, key.encode())
        elif isinstance(value, list):
            self.lib.lua_createtable(state, len(value), 0)
            for index, item in enumerate(value, 1):
                self.push(state, item)
                self.lib.lua_rawseti(state, -2, index)
        else:
            raise TypeError("Unsupported test value type: " + type(value).__name__)

    def string(self, state, index=-1):
        length = ctypes.c_size_t()
        pointer = self.lib.lua_tolstring(state, index, ctypes.byref(length))
        if not pointer:
            raise AssertionError("Lua did not return a string")
        return ctypes.string_at(pointer, length.value).decode()

    def evaluate(self, script, obj):
        state = self.lib.luaL_newstate()
        if not state:
            raise MemoryError("Could not allocate Lua state")
        try:
            self.lib.luaL_openlibs(state)
            self.push(state, obj)
            self.lib.lua_setglobal(state, b"obj")
            loaded = self.lib.luaL_loadbufferx(
                state, script, len(script), b"@gate-health.lua", b"t")
            if loaded:
                raise AssertionError("Lua compilation failed: " + self.string(state))
            if self.lib.lua_pcallk(state, 0, 1, 0, 0, None):
                raise AssertionError("Lua execution failed: " + self.string(state))
            if self.lib.lua_type(state, -1) != 5:  # LUA_TTABLE, shared by both versions.
                raise AssertionError("Lua health result must be a table")
            result = {}
            for key in ("status", "message"):
                self.lib.lua_getfield(state, -1, key.encode())
                result[key] = self.string(state)
                self.lib.lua_settop(state, -2)
            return result
        finally:
            self.lib.lua_close(state)


def gate(state="Pending", version=""):
    identifier = f"example-app-{PROJECT}-45678"
    return {
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {
            "name": "preview-registry-ready", "namespace": "test-" + identifier,
            "resourceVersion": "1",
            "labels": {"company.example/preview-id": identifier,
                       "company.example/source-project-id": PROJECT,
                       "company.example/ephemeral": "true",
                       "company.example/registry-gate": "true"},
            "annotations": {"argocd.argoproj.io/sync-wave": "-10"},
        },
        "data": {"state": state, "sourceResourceVersion": version},
    }


@unittest.skipUnless(LIBRARIES, "Lua 5.2/5.4 shared libraries are unavailable")
class GateHealthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = SCRIPT.read_bytes()
        cls.runtimes = [(version, LuaRuntime(version, library))
                        for version, library in LIBRARIES]

    def assert_status(self, obj, expected):
        for version, runtime in self.runtimes:
            with self.subTest(lua=version):
                result = runtime.evaluate(self.script, copy.deepcopy(obj))
                self.assertIsInstance(result["message"], str)
                self.assertEqual(result["status"], expected)

    def test_unrelated_configmaps_remain_healthy(self):
        for obj in ({}, {"metadata": {"name": "ordinary-config"}},
                    {"metadata": {"name": "ordinary-config", "labels": {
                        "company.example/registry-gate": "true"}}},
                    {"metadata": {"name": "preview-registry-ready",
                                  "namespace": "production"}}):
            with self.subTest(obj=obj):
                self.assert_status(obj, "Healthy")

    def test_pending_owned_gate_is_progressing(self):
        self.assert_status(gate(), "Progressing")

    def test_valid_ready_gate_is_healthy(self):
        self.assert_status(gate("Ready", "101"), "Healthy")

    def test_ready_with_empty_source_version_never_becomes_healthy(self):
        for version, runtime in self.runtimes:
            with self.subTest(lua=version):
                result = runtime.evaluate(self.script, gate("Ready", ""))
                self.assertIn(result["status"], {"Degraded", "Progressing"})

    def test_exact_data_contract_rejects_extra_missing_or_invalid_values(self):
        variants = [None, {}, {"state": "Ready"}, {"sourceResourceVersion": "1"},
                    {"state": "Ready", "sourceResourceVersion": "1", "extra": "bad"},
                    {"state": "Unknown", "sourceResourceVersion": "1"},
                    {"state": "Ready", "sourceResourceVersion": 1},
                    {"state": True, "sourceResourceVersion": "1"}]
        for data in variants:
            obj = gate("Ready", "1")
            obj["data"] = data
            with self.subTest(data=data):
                self.assert_status(obj, "Degraded")

    def test_invalid_identity_namespace_or_ownership_is_degraded(self):
        changes = [("company.example/preview-id", None),
                   ("company.example/source-project-id", OTHER_PROJECT),
                   ("company.example/ephemeral", "false"),
                   ("company.example/registry-gate", None),
                   ("company.example/registry-gate", "false")]
        for key, value in changes:
            obj = gate("Ready", "1")
            obj["metadata"]["labels"][key] = value
            with self.subTest(label=key, value=value):
                self.assert_status(obj, "Degraded")
        for identifier in ("production", f"example-app-{OTHER_PROJECT}-45678", f"example-app-{PROJECT}-0",
                           f"example-app-{PROJECT}-045678", f"example-app-{PROJECT}-45678-suffix"):
            obj = gate("Ready", "1")
            obj["metadata"]["labels"]["company.example/preview-id"] = identifier
            obj["metadata"]["namespace"] = "test-" + identifier
            with self.subTest(identifier=identifier):
                self.assert_status(obj, "Degraded")
        obj = gate("Ready", "1")
        obj["metadata"]["namespace"] = "production"
        self.assert_status(obj, "Degraded")

    def test_foreign_tracking_or_extra_labels_are_degraded(self):
        for key, value in (("foreign", "value"),
                           ("app.kubernetes.io/instance", "another-preview"),
                           ("app.kubernetes.io/part-of", "another-platform")):
            obj = gate("Ready", "1")
            obj["metadata"]["labels"][key] = value
            with self.subTest(key=key):
                self.assert_status(obj, "Degraded")

    def test_expected_argo_tracking_labels_and_annotations_are_healthy(self):
        obj = gate("Ready", "1")
        obj["metadata"]["labels"].update({
            "app.kubernetes.io/instance": f"example-app-{PROJECT}-45678",
            "app.kubernetes.io/part-of": "preview-platform"})
        obj["metadata"]["annotations"]["argocd.argoproj.io/tracking-id"] = (
            f"example-app-{PROJECT}-45678:/ConfigMap:test-example-app-{PROJECT}-45678/preview-registry-ready")
        self.assert_status(obj, "Healthy")

    def test_terminating_foreign_owned_or_immutable_gate_is_degraded(self):
        for key, value in (("deletionTimestamp", "2026-11-10T00:00:00Z"),
                           ("ownerReferences", [{"uid": "foreign"}]),
                           ("finalizers", ["foreign/finalizer"])):
            obj = gate("Ready", "1")
            obj["metadata"][key] = value
            with self.subTest(metadata=key):
                self.assert_status(obj, "Degraded")
        obj = gate("Ready", "1")
        obj["immutable"] = True
        self.assert_status(obj, "Degraded")

    def test_binary_data_cannot_bypass_the_registry_gate(self):
        obj = gate("Ready", "1")
        obj["binaryData"] = {"unexpected": "dGVzdA=="}
        self.assert_status(obj, "Degraded")


if __name__ == "__main__":
    unittest.main()
