"""Values in the Kubernetes example that the API server checks but kubeconform does not.

CI validates the rendered overlays with kubeconform, which checks fields and types only. An
ingress host such as ``chatbot.YOUR_DOMAIN`` passed it but was rejected by the API server
(hosts must be lowercase RFC 1123 names), so ``kubectl apply --dry-run=server`` always failed.
"""

import re

import yaml

from tests.conftest import ROOT

K8S = ROOT / "deploy" / "k8s"
RFC1123 = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*")


def _ingresses() -> list[tuple[str, dict]]:
    out = []
    for path in sorted(K8S.rglob("*.yaml")):
        for obj in yaml.safe_load_all(path.read_text()):
            if isinstance(obj, dict) and obj.get("kind") == "Ingress":
                out.append((str(path.relative_to(ROOT)), obj))
    return out


def test_ingress_hosts_are_valid_dns_names():
    found = _ingresses()
    assert found, "no Ingress found under deploy/k8s"
    for where, ing in found:
        spec = ing["spec"]
        hosts = [r["host"] for r in spec.get("rules", []) if "host" in r]
        hosts += [h for t in spec.get("tls", []) for h in t.get("hosts", [])]
        for host in hosts:
            assert RFC1123.fullmatch(host) and len(host) <= 253, f"{where}: bad host {host!r}"
