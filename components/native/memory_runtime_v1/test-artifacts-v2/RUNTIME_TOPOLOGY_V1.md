# Runtime Topology V1

{
  "version": "memory-runtime-v1-shadow-20260919",
  "created_at": "2026-09-19T11:10:37.087988Z",
  "production_behavior": "UNCHANGED",
  "native_live_memory_context": "OFF",
  "nodes": [
    {
      "component": "Telegram",
      "code_path": "Hermes Telegram adapter",
      "service": "hermes-gateway.service",
      "authority": "transport",
      "failure_semantics": "Hermes-owned"
    },
    {
      "component": "Hermes Gateway",
      "code_path": "./native",
      "service": "hermes-gateway.service",
      "input": "Telegram update",
      "output": "provider request/response and state.db persistence",
      "persistence": "./data/persona/state.db",
      "authority": "legacy production",
      "restart_semantics": "systemd restart"
    },
    {
      "component": "Native V0",
      "code_path": "./native/app",
      "service": "chiyo-native-v0.service",
      "input": "native HTTP/Telegram test input",
      "output": "raw native conversation and M0 evidence",
      "persistence": "/var/lib/chiyo-native-v0",
      "authority": "Native staging",
      "failure_semantics": "isolated"
    },
    {
      "component": "M1/M2/M3",
      "code_path": "./native/*_worker.py",
      "service": "shadow workers",
      "input": "M0 evidence",
      "output": "episodes/understandings/recall shadow",
      "persistence": "/var/lib/chiyo-native-v0/memory",
      "authority": "shadow only",
      "failure_semantics": "does not gate Hermes"
    },
    {
      "component": "Memory Runtime V1",
      "code_path": "./native",
      "service": "not enabled",
      "input": "read-only Hermes/native shadow observations",
      "output": "hypothetical manifests/capsules/reports",
      "persistence": "V1 artifact directory",
      "authority": "shadow only",
      "failure_semantics": "fail closed"
    }
  ],
  "services": [
    {
      "service": "hermes-gateway.service",
      "returncode": 125,
      "stderr_class": "str"
    },
    {
      "service": "chiyo-native-v0.service",
      "returncode": 125,
      "stderr_class": "str"
    },
    {
      "service": "chiyo-native-m1-shadow.service",
      "returncode": 125,
      "stderr_class": "str"
    },
    {
      "service": "chiyo-native-m2-shadow.service",
      "returncode": 125,
      "stderr_class": "str"
    },
    {
      "service": "chiyo-native-m3-shadow.service",
      "returncode": 125,
      "stderr_class": "str"
    },
    {
      "service": "chiyo-native-telegram-test.service",
      "returncode": 125,
      "stderr_class": "str"
    },
    {
      "service": "chiyo-memory-ombre-shadow.service",
      "returncode": 125,
      "stderr_class": "str"
    },
    {
      "service": "chiyo-memory-recall-shadow.service",
      "returncode": 125,
      "stderr_class": "str"
    }
  ],
  "prohibited_edges": [
    "V1 hypothetical capsule -> real provider request",
    "V1 shadow -> Hermes mutation",
    "V1 shadow -> Native Memory Store write",
    "Telegram test bot -> Hermes main bot"
  ]
}
