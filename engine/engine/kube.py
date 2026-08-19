"""Live Kubernetes API connectivity probe for the cluster "Test" action.

Resolves credentials — the engine's own in-cluster ServiceAccount, or a referenced
k8s Secret (by auth_method) — and calls the API server's `/version` endpoint to verify
reachability + auth. Returns a small dict the API/UI render. stdlib only (urllib + ssl)
plus PyYAML for kubeconfig parsing; no heavy Kubernetes client dependency.

Supported auth_method values, and where the credential comes from (the k8s Secret named
by `credential_ref`, read via the engine's in-cluster ServiceAccount):
  * (none) / in-cluster : the engine's mounted SA token -> probe the cluster it runs in.
  * token               : Secret key `token`            -> Bearer.
  * kubeconfig          : Secret key `kubeconfig`/`config` -> server + token/cert from current-context.
  * client_cert         : Secret keys `tls.crt` + `tls.key` -> mutual TLS.
  * basic               : Secret keys `username` + `password` -> Basic auth.
"""
from __future__ import annotations

import base64
import json
import os
import ssl
import tempfile
import urllib.request
from typing import Optional

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"


# --- in-cluster ServiceAccount ---------------------------------------------

def _incluster_server() -> Optional[str]:
    host = os.environ.get("KUBERNETES_SERVICE_HOST")
    if not host:
        return None
    return f"https://{host}:{os.environ.get('KUBERNETES_SERVICE_PORT', '443')}"


def _read(path: str) -> Optional[str]:
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return None


def _sa_token() -> Optional[str]:
    return _read(f"{SA_DIR}/token")


def _sa_ca() -> Optional[str]:
    p = f"{SA_DIR}/ca.crt"
    return p if os.path.exists(p) else None


def _sa_namespace() -> str:
    return _read(f"{SA_DIR}/namespace") or "default"


# --- HTTP + TLS helpers -----------------------------------------------------

def _write_tmp(content: str, tmp: list) -> str:
    fh = tempfile.NamedTemporaryFile("w", delete=False, suffix=".pem")
    fh.write(content)
    fh.close()
    tmp.append(fh.name)
    return fh.name


def _http_get(url, headers=None, ca_file=None, client_cert=None, insecure=False, timeout=8):
    if insecure or not ca_file:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    else:
        ctx = ssl.create_default_context(cafile=ca_file)
    if client_cert:
        ctx.load_cert_chain(certfile=client_cert[0], keyfile=client_cert[1])
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, context=ctx, timeout=timeout) as r:  # noqa: S310 (user-configured endpoint)
        return r.status, r.read()


def _read_secret(server, token, ca_file, namespace, name) -> dict:
    url = f"{server}/api/v1/namespaces/{namespace}/secrets/{name}"
    _, body = _http_get(url, headers={"Authorization": f"Bearer {token}"}, ca_file=ca_file)
    data = (json.loads(body) or {}).get("data") or {}
    return {k: base64.b64decode(v).decode("utf-8", "replace") for k, v in data.items()}


def _parse_kubeconfig(text: str):
    """Return (server, token, client_cert_tuple_or_None, ca_pem_or_None) from a kubeconfig."""
    import yaml
    kc = yaml.safe_load(text) or {}
    ctx_name = kc.get("current-context")
    ctx = next((c["context"] for c in kc.get("contexts", []) if c.get("name") == ctx_name), {})
    cluster = next((c["cluster"] for c in kc.get("clusters", []) if c.get("name") == ctx.get("cluster")), {})
    user = next((u["user"] for u in kc.get("users", []) if u.get("name") == ctx.get("user")), {})
    server = cluster.get("server")
    ca = _b64_or_none(cluster.get("certificate-authority-data"))
    token = user.get("token")
    crt = _b64_or_none(user.get("client-certificate-data"))
    key = _b64_or_none(user.get("client-key-data"))
    return server, token, ((crt, key) if crt and key else None), ca


def _b64_or_none(v):
    if not v:
        return None
    try:
        return base64.b64decode(v).decode("utf-8", "replace")
    except Exception:
        return v


# --- the probe --------------------------------------------------------------

def probe(api_url=None, auth_method=None, credential_ref=None, ca_cert=None, namespace=None) -> dict:
    """Attempt to reach the cluster API. Returns
    {reachable: bool, server?: str, server_version?: str, detail?: str}."""
    namespace = namespace or _sa_namespace()
    server = (api_url or "").rstrip("/") or _incluster_server()
    if not server:
        return {"reachable": False, "detail": "no api_url provided and engine is not running in-cluster"}

    headers: dict = {}
    client_cert = None
    ca_file = None
    insecure = False
    tmp: list = []
    sa_ca = _sa_ca()
    try:
        if credential_ref:
            sa_tok, in_srv = _sa_token(), _incluster_server()
            if not (sa_tok and in_srv):
                return {"reachable": False, "detail": "cannot read credential Secret: engine is not running in-cluster"}
            try:
                secret = _read_secret(in_srv, sa_tok, sa_ca, namespace, credential_ref)
            except Exception as exc:
                return {"reachable": False, "detail": f"reading Secret {namespace}/{credential_ref}: {exc}"}
            am = auth_method or "token"
            if am == "token":
                headers["Authorization"] = "Bearer " + (secret.get("token") or "").strip()
            elif am == "basic":
                up = f"{secret.get('username','')}:{secret.get('password','')}"
                headers["Authorization"] = "Basic " + base64.b64encode(up.encode()).decode()
            elif am == "client_cert":
                crt = secret.get("tls.crt") or _b64_or_none(secret.get("client-certificate-data"))
                key = secret.get("tls.key") or _b64_or_none(secret.get("client-key-data"))
                if not (crt and key):
                    return {"reachable": False, "detail": "client_cert Secret missing tls.crt/tls.key"}
                client_cert = (_write_tmp(crt, tmp), _write_tmp(key, tmp))
            elif am == "kubeconfig":
                kc = secret.get("kubeconfig") or secret.get("config") or ""
                if not kc:
                    return {"reachable": False, "detail": "kubeconfig Secret missing a 'kubeconfig' key"}
                srv, token, cc, kca = _parse_kubeconfig(kc)
                if srv and not api_url:
                    server = srv.rstrip("/")
                if token:
                    headers["Authorization"] = "Bearer " + token
                if cc:
                    client_cert = (_write_tmp(cc[0], tmp), _write_tmp(cc[1], tmp))
                if kca and not ca_cert:
                    ca_cert = kca
            else:
                return {"reachable": False, "detail": f"unsupported auth_method {am!r}"}
        else:
            tok = _sa_token()
            if tok:
                headers["Authorization"] = "Bearer " + tok

        # TLS verification: explicit ca_cert > in-cluster SA CA (for the local API) > insecure (dev).
        if ca_cert:
            ca_file = _write_tmp(ca_cert, tmp)
        elif sa_ca and server == _incluster_server():
            ca_file = sa_ca
        else:
            insecure = True

        try:
            status, body = _http_get(server + "/version", headers=headers, ca_file=ca_file,
                                     client_cert=client_cert, insecure=insecure)
        except Exception as exc:
            return {"reachable": False, "server": server, "detail": str(exc)}

        if status == 200:
            ver = ""
            try:
                ver = (json.loads(body) or {}).get("gitVersion", "")
            except Exception:
                pass
            return {"reachable": True, "server": server, "server_version": ver}
        if status in (401, 403):
            # We reached the API server but the credentials lack access — still "connected".
            return {"reachable": True, "server": server,
                    "detail": f"reached API server (HTTP {status}); credentials may lack read access"}
        return {"reachable": False, "server": server, "detail": f"HTTP {status} from /version"}
    finally:
        for f in tmp:
            try:
                os.unlink(f)
            except Exception:
                pass
