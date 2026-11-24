"""Zero-token Khazix-inspired advisory dispatch engine for 4-host orchestration."""
from fnmatch import fnmatchcase
import os
import re
from typing import Any

import routing_policy
import storage as st

WORKFLOWS: tuple[str, ...] = (
  "auto",
  "dual-plan",
  "frontend",
  "copy-ux",
  "backend-module",
  "gate-review",
  "bulk-cheap",
  "browser-research",
)
HOSTS: tuple[str, ...] = ("claude", "codex", "antigravity", "dsh")
INLINE_LINE_THRESHOLD: int = 40
MAX_REVIEW_ROUNDS: int = 2

SENSITIVE_PATH_TOKENS: tuple[str, ...] = (
  "auth",
  "oauth",
  "sso",
  "login",
  "signin",
  "token",
  "jwt",
  "secret",
  "cred",
  "credential",
  "permission",
  "rbac",
  "acl",
  "billing",
  "invoice",
  "ledger",
  "wallet",
  "payment",
  "payout",
  "money",
  "feishu",
  "lark",
  "webhook",
  "publish",
  "prod-config",
)

_COMPOUND_SENSITIVE_PATHS: tuple[str, ...] = (
  "prod-config",
  "prod_config",
  "prod/config",
  "config/prod",
  "production/config",
)
_EXACT_PATH_WORDS: frozenset[str] = frozenset({
  *SENSITIVE_PATH_TOKENS,
  "tokens",
  "secrets",
  "creds",
  "credentials",
  "permissions",
  "perms",
  "payments",
  "payouts",
  "webhooks",
  "authn",
  "authz",
})
_SENSITIVE_PATH_PREFIXES: tuple[str, ...] = (
  "authenticat",
  "authoriz",
  "credential",
  "permission",
  "webhook",
  "payment",
  "billing",
  "feishu",
)
_SENSITIVE_TASK_PATTERNS: tuple[re.Pattern[str], ...] = (
  re.compile(
    r"\b("
    r"auth(?:entication|orization|n|z)?|oauth|sso|login|sign-?in|"
    r"(?:access|refresh|bearer|session|auth|api|csrf)[ _-]tokens?|token[ _-]rotation|"
    r"jwt|secrets?|credentials?|api[ _-]keys?|"
    r"permissions?|privileges?|rbac|acl|"
    r"billing|payments?|payouts?|invoices?|ledger|wallet|money|financial|"
    r"feishu|lark|webhooks?|publish(?:ing)?|"
    r"prod[-_ ]config|production[ _-]config|external[ _-]messages?"
    r")\b",
    re.IGNORECASE,
  ),
  re.compile(
    r"(算钱|涉钱|计费|支付|账单|金额|余额|打款|权限|越权|鉴权|登录|密钥|凭据|飞书|对外发消息|发消息|生产配置|线上配置)"
  ),
)

_HOST_ALIASES: dict[str, str] = {
  "claude": "claude",
  "claude-code": "claude",
  "anthropic": "claude",
  "codex": "codex",
  "gpt": "codex",
  "openai": "codex",
  "antigravity": "antigravity",
  "agy": "antigravity",
  "gemini": "antigravity",
  "dsh": "dsh",
  "deepseek": "dsh",
  "deepseek-company": "dsh",
  "deepseek-official": "dsh",
}
_WORKFLOW_ALIASES: dict[str, str] = {w.replace("-", "_"): w for w in WORKFLOWS}

_FRONTEND_EXTS: frozenset[str] = frozenset({".tsx", ".jsx", ".vue", ".svelte", ".css", ".scss", ".less", ".html"})
_BACKEND_EXTS: frozenset[str] = frozenset({".py", ".go", ".rs", ".java", ".kt", ".cpp", ".cc", ".c", ".sql", ".proto"})
_DOC_EXTS: frozenset[str] = frozenset({".md", ".mdx", ".rst", ".txt"})

_BLIND_CONTRACT: str = (
  "BLIND SECOND OPINION: Form an independent plan from raw user intent and verified repo facts only. "
  "Ignore parent drafts/preferences; verify claims in code and tag unverified assumptions [speculative]."
)
_REVIEW_GATE_CONTRACT: str = (
  "2-TIER GATE REVIEW: Classify findings as BLOCKING or ADVISORY. "
  "BLOCKING = only concrete money/math error, data loss, auth/permission bypass, wrong external message/mutation, "
  "or release-breaking crash with exact trigger path. All else = ADVISORY (non-blocking)."
)

_RE_REVIEW_FIX = re.compile(
  r"(\b(?:fix|address|apply|resolve)(?:[ _-]+[A-Za-z0-9_-]+){0,3}[ _-]+review[ _-]+"
  r"(?:comments?|feedback|findings?|issues?|nits?)\b"
  r"|\breview[ _-]+nits?\b|修审查|改审查|修复审查)",
  re.IGNORECASE,
)
_RE_TIGHT_COUPLING = re.compile(
  r"(ui[ _]*<->[ _]*api|ui[-_ ]api[ _-]+coupling|tight(?:ly)?[ _-]+coupled[ _-]+(?:ui|frontend|page)"
  r"|coupled[ _-]+ui[ _-]+and[ _-]+api|wire[ _-]+(?:ui|page|form)[ _-]+to[ _-]+api"
  r"|back[-_ ]and[-_ ]forth[ _-]+with[ _-]+(?:page|ui|api)|来回对接口|前后端联调|页面来回对接口)",
  re.IGNORECASE,
)
_RE_TRIVIAL = re.compile(
  r"(\bfix[ _-]+(?:a[ _-]+)?typo\b|\bone[-_ ]line[ _-]+fix\b|\bsingle[-_ ]line\b|\brename[ _-]+one\b"
  r"|错别字|只改几个字|顺手改一下)",
  re.IGNORECASE,
)
_RE_GATE_REVIEW = re.compile(
  r"(\b(?:pre-?merge|gate|code|pr|merge|security|release)(?:[ _-]+[A-Za-z0-9_-]+){0,2}[ _-]+(?:review|audit)\b"
  r"|\b(?:review|audit)(?:[ _-]+[A-Za-z0-9_-]+){0,2}[ _-]+(?:diff|pr|changes|patch)\b"
  r"|代码审查|合并前.*审查|上线前.*审查|把关审查)",
  re.IGNORECASE,
)
_RE_DUAL_PLAN = re.compile(
  r"(\b(?:architecture|system[ _-]+design|schema[ _-]+design|migration[ _-]+plan|redesign|roadmap|rfc|adr)\b"
  r"|\bsecond[ _-]+opinion[ _-]+plan\b|架构|大方案|大规划|整体怎么改|数据怎么存|技术方案)",
  re.IGNORECASE,
)
_RE_COPY_UX = re.compile(
  r"(\b(?:ui[ _-]+copy|ux[ _-]+copy|microcopy|ux[ _-]+writing|onboarding[ _-]+copy|empty[-_ ]state[ _-]+(?:copy|text)"
  r"|interaction[ _-]+(?:copy|text|wording)|polish[ _-]+(?:ui[ _-]+)?(?:copy|wording|labels?))\b"
  r"|文案|交互文案|界面文案|空状态|提示语)",
  re.IGNORECASE,
)
_RE_BROWSER = re.compile(
  r"(\b(?:playwright|browser[ _-]+(?:automation|control|flow)|multi-?site[ _-]+research|web[ _-]+scraping"
  r"|cross-?site[ _-]+(?:flow|research)|logged-?in[ _-]+browser)\b|浏览器操控|网页操控|跨网站|大调研)",
  re.IGNORECASE,
)
_RE_FRONTEND = re.compile(
  r"(\b(?:frontend|front-end|visual[ _-]+design|hero[ _-]+section|motion[ _-]+design|keyframe|animation"
  r"|tailwind|css[ _-]+layout|landing[ _-]+page|ui[ _-]+component|theme[ _-]+styling)\b"
  r"|前端|视觉|动效|主视觉|配色|页面样式)",
  re.IGNORECASE,
)
_RE_BULK_CHEAP = re.compile(
  r"(\b(?:changelog|docstrings?|readme|documentation[ _-]+sync|mechanical[ _-]+(?:rename|refactor|cleanup)"
  r"|boilerplate|bulk[ _-]+(?:scan|update|check)|explore[ _-]+codebase)\b"
  r"|文档整理|批量整理|同步文档|机械修改)",
  re.IGNORECASE,
)
_RE_SIMPLE_BACKEND = re.compile(
  r"(\b(?:simple|small[ _-]+helper|fixture|boilerplate|format[ _-]+helper|minor[ _-]+utility)\b|简单辅助)",
  re.IGNORECASE,
)
_RE_COMPLEX_BACKEND = re.compile(
  r"(\b(?:lock-?free|consensus|distributed[ _-]+transaction|formal[ _-]+invariant|compiler[ _-]+pass"
  r"|cryptographic|memory[ _-]+model|multi-?node[ _-]+replication)\b|分布式一致性|无锁并发)",
  re.IGNORECASE,
)


def normalize_host(caller_host: str | None = "claude") -> str:
  """Normalize a caller host identifier or auto-detect from environment variables.

  Args:
    caller_host: Host name, alias, ``auto``, or ``None``.

  Returns:
    Canonical host string in ``HOSTS`` (``claude``, ``codex``, ``antigravity``, ``dsh``).

  Raises:
    ValueError: If ``caller_host`` is not a recognized host or alias.
  """
  raw = "" if caller_host is None else caller_host
  if not isinstance(raw, str):
    raise ValueError("caller_host must be a string")
  cleaned = raw.strip().lower()
  if not cleaned or cleaned == "auto":
    env_host = os.environ.get("DELEGATE_CALLER_HOST", "").strip().lower()
    if env_host and env_host in _HOST_ALIASES:
      return _HOST_ALIASES[env_host]
    if os.environ.get("ANTIGRAVITY_CONVERSATION_ID"):
      return "antigravity"
    if os.environ.get("CODEX_THREAD_ID"):
      return "codex"
    if os.environ.get("DSH_SESSION_ID"):
      return "dsh"
    return "claude"
  resolved = _HOST_ALIASES.get(cleaned)
  if resolved is None:
    raise ValueError("caller_host must be auto/claude/codex/antigravity/agy/dsh")
  return resolved


def normalize_workflow(workflow: str | None = "auto") -> str:
  """Validate and canonicalize an advisory workflow name.

  Args:
    workflow: Workflow identifier, underscore alias, or ``None``.

  Returns:
    Canonical workflow name in ``WORKFLOWS``.

  Raises:
    ValueError: If ``workflow`` is not in ``WORKFLOWS``.
  """
  if workflow is None or workflow == "":
    return "auto"
  if not isinstance(workflow, str):
    raise ValueError("workflow must be one of: " + ", ".join(WORKFLOWS))
  cleaned = workflow.strip().lower()
  resolved = _WORKFLOW_ALIASES.get(cleaned, cleaned)
  if resolved not in WORKFLOWS:
    raise ValueError("workflow must be one of: " + ", ".join(WORKFLOWS))
  return resolved


def _path_is_sensitive(raw_path: str) -> bool:
  """Return True when a single file path touches sensitive auth/money/external/prod scope."""
  lowered = raw_path.strip().lower().replace("\\", "/")
  if not lowered:
    return False
  if any(token in lowered for token in _COMPOUND_SENSITIVE_PATHS):
    return True
  parts = [p for p in re.split(r"[/._-]+", lowered) if p]
  for part in parts:
    if part in _EXACT_PATH_WORDS or part.startswith(_SENSITIVE_PATH_PREFIXES):
      return True
  if ("prod" in parts or "production" in parts) and ("config" in parts or "settings" in parts or "env" in parts):
    return True
  return False


def is_sensitive_scope(paths: list[str] | None, task: str = "") -> bool:
  """Detect auth, money/billing, permission, external-message, or prod-config scope.

  Args:
    paths: Optional list of required or output file paths.
    task: Optional task description string.

  Returns:
    True when deep ``xhigh`` review scrutiny is warranted, False otherwise.

  Raises:
    ValueError: If ``paths`` or ``task`` has an invalid type.
  """
  if paths is not None and (not isinstance(paths, list) or any(not isinstance(p, str) for p in paths)):
    raise ValueError("paths must be a list of strings or None")
  if not isinstance(task, str):
    raise ValueError("task must be a string")
  if paths and any(_path_is_sensitive(path) for path in paths):
    return True
  if task:
    if any(pattern.search(task) for pattern in _SENSITIVE_TASK_PATTERNS):
      return True
    for candidate in re.findall(r"[A-Za-z0-9_./\\-]{4,}", task):
      if ("/" in candidate or candidate.endswith((".py", ".ts", ".js", ".go", ".yml", ".yaml", ".json"))) and (
        _path_is_sensitive(candidate)
      ):
        return True
  return False


def _file_exts(paths: list[str]) -> set[str]:
  """Extract lowercase file suffixes from path strings."""
  exts: set[str] = set()
  for item in paths:
    slash = max(item.rfind("/"), item.rfind("\\"))
    base = item[slash + 1:]
    dot = base.rfind(".")
    if dot > 0:
      exts.add(base[dot:].lower())
  return exts


def _infer_workflow(
  task: str,
  profile: dict[str, Any] | None,
  paths: list[str],
  review_round: int,
) -> str:
  """Classify ``workflow='auto'`` deterministically in zero model tokens."""
  kind = (profile or {}).get("kind", "")
  if _RE_REVIEW_FIX.search(task) and kind != "review":
    return "backend-module"
  if review_round >= MAX_REVIEW_ROUNDS and (kind in {"", "review"} or _RE_GATE_REVIEW.search(task)):
    return "gate-review"
  if kind == "review" or _RE_GATE_REVIEW.search(task):
    return "gate-review"
  if kind == "architecture" or _RE_DUAL_PLAN.search(task):
    return "dual-plan"
  if _RE_COPY_UX.search(task):
    return "copy-ux"
  if _RE_BROWSER.search(task):
    return "browser-research"
  exts = _file_exts(paths)
  has_fe = bool(exts & _FRONTEND_EXTS)
  has_be = bool(exts & _BACKEND_EXTS)
  if (_RE_FRONTEND.search(task) or (has_fe and not has_be)) and not _RE_TIGHT_COUPLING.search(task):
    return "frontend"
  if (
    kind in {"documentation", "mechanical", "lookup"}
    or (kind == "research" and (profile or {}).get("risk") == "low" and (profile or {}).get("uncertainty") == "low")
    or _RE_BULK_CHEAP.search(task)
    or (bool(exts) and exts <= _DOC_EXTS and kind not in {"architecture", "review"})
  ):
    return "bulk-cheap"
  return "backend-module"


def _agent_usable(
  name: str,
  agents: dict[str, Any],
  providers: dict[str, Any],
  explicit_pool: bool,
) -> bool:
  """Check whether a role is enabled (or implicitly available before config sync)."""
  if name not in agents:
    return not explicit_pool and name in {"agy-advisor", "agy-builder"}
  role = agents[name]
  if not isinstance(role, dict) or role.get("enabled") is False:
    return False
  prov_name = role.get("provider", "")
  if prov_name and prov_name in providers and providers[prov_name].get("enabled") is False:
    return False
  return True


def _denied_patterns(role: dict[str, Any], provider: dict[str, Any], cfg: dict[str, Any]) -> list[str]:
  """Collect active ``deny_models`` globs across global, provider, and role scopes."""
  global_pol = cfg.get("model_policy") or {}
  return [
    *(global_pol.get("deny_models") or []),
    *(provider.get("deny_models") or []),
    *(role.get("deny_models") or []),
  ]


def _allowed(model: str, patterns: list[str]) -> bool:
  """Return True when ``model`` does not match any deny pattern."""
  return not any(fnmatchcase(model, pat) for pat in patterns)


def _known_models(provider_name: str, role: dict[str, Any], provider: dict[str, Any]) -> list[str]:
  """Read locally known models from role, provider, or cached catalog without subprocesses."""
  if role.get("models"):
    return [str(m) for m in role["models"]]
  if provider.get("models"):
    return [str(m) for m in provider["models"]]
  cached = st.read("catalog-" + provider_name + ".json") if provider_name else None
  if isinstance(cached, dict) and isinstance(cached.get("models"), list):
    out: list[str] = []
    for item in cached["models"]:
      if isinstance(item, str):
        out.append(item)
      elif isinstance(item, dict):
        mid = item.get("model") or item.get("id")
        if isinstance(mid, str):
          out.append(mid)
    return out
  return []


def _pick_model(
  preferred: tuple[str, ...],
  family_token: str,
  provider_name: str,
  role: dict[str, Any],
  provider: dict[str, Any],
  cfg: dict[str, Any],
  fallback: str,
) -> str:
  """Select the highest-priority permitted model from known catalog or safe fallback."""
  patterns = _denied_patterns(role, provider, cfg)
  known = [m for m in _known_models(provider_name, role, provider) if _allowed(m, patterns)]
  if known:
    for cand in preferred:
      if cand in known:
        return cand
    for item in known:
      if family_token and family_token in item.lower():
        return item
    return known[0]
  for cand in preferred:
    if _allowed(cand, patterns):
      return cand
  return fallback


def _pick_deepseek_agent(
  write: bool,
  agents: dict[str, Any],
  providers: dict[str, Any],
) -> tuple[str, str] | None:
  """Find an enabled DeepSeek/company role and its flash default model."""
  target_ws = "worktree" if write else "read-only"
  preferred = (
    ("deepseek-company-builder", "company-builder", "deepseek-official-builder")
    if write else ("deepseek-company-reviewer", "company-reviewer", "deepseek-official-reviewer", "deepseek-docs")
  )
  ordered = list(preferred) + [k for k in agents if k not in preferred]
  for name in ordered:
    role = agents.get(name)
    if not isinstance(role, dict) or role.get("enabled") is False:
      continue
    if role.get("workspace", "read-only") != target_ws:
      continue
    prov_name = str(role.get("provider", ""))
    prov = providers.get(prov_name, {})
    if prov.get("enabled") is False:
      continue
    models = [str(m) for m in (role.get("models") or prov.get("models") or [])]
    default_m = str(role.get("default_model") or prov.get("default_model") or "")
    is_ds = (
      "deepseek" in name.lower()
      or "deepseek" in prov_name.lower()
      or "deepseek-flash" in models
      or "deepseek" in default_m.lower()
    )
    if is_ds:
      chosen = "deepseek-flash" if (not models or "deepseek-flash" in models) else (default_m or models[0])
      return name, chosen
  return None


def _pack(
  action: str,
  workflow: str,
  agent: str | None,
  model: str | None,
  effort: str | None,
  *,
  blind_context: bool = False,
  review_gate: bool = False,
  sensitive_scope: bool = False,
  reason: str,
) -> dict[str, Any]:
  """Build a compact <=120-token recommendation dictionary."""
  return {
    "action": action,
    "workflow": workflow,
    "agent": agent,
    "model": model,
    "effort": effort,
    "blind_context": blind_context,
    "review_gate": review_gate,
    "max_review_rounds": MAX_REVIEW_ROUNDS,
    "sensitive_scope": sensitive_scope,
    "reason": reason[:110],
  }


def recommend(
  task: str,
  *,
  caller_host: str = "claude",
  workflow: str = "auto",
  task_profile: dict[str, Any] | None = None,
  required_files: list[str] | None = None,
  output_files: list[str] | None = None,
  estimated_lines: int | None = None,
  configured_agents: dict[str, Any] | None = None,
  review_round: int = 0,
) -> dict[str, Any]:
  """Recommend inline execution, blind dual-opinion, delegation, or user escalation in 0 tokens.

  Args:
    task: Task description or user request text.
    caller_host: Host environment initiating the work (``claude``, ``codex``, ``antigravity``, ``dsh``).
    workflow: Explicit workflow from ``WORKFLOWS`` or ``auto``.
    task_profile: Optional structured subtask assessment dictionary.
    required_files: Optional required input file paths.
    output_files: Optional expected output file paths.
    estimated_lines: Optional estimated changed line count for break-even gating.
    configured_agents: Optional configured agent dictionary override.
    review_round: Prior ``needs-work`` review rounds completed on this item.

  Returns:
    Compact dictionary (<=120 serialized tokens) with ``action``, ``workflow``, ``agent``,
    ``model``, ``effort``, ``blind_context``, ``review_gate``, ``max_review_rounds``,
    ``sensitive_scope``, and ``reason``.

  Raises:
    ValueError: If host, workflow, line estimate, review round, or profile inputs are invalid.
  """
  text = "" if task is None else task
  if not isinstance(text, str):
    raise ValueError("task must be a string")
  if estimated_lines is not None and (type(estimated_lines) is not int or estimated_lines < 0):
    raise ValueError("estimated_lines must be a non-negative integer or None")
  if type(review_round) is not int or review_round < 0:
    raise ValueError("review_round must be a non-negative integer")
  host = normalize_host(caller_host)
  wf_req = normalize_workflow(workflow)
  profile = routing_policy.normalize(task_profile)
  paths = [*(required_files or []), *(output_files or [])]
  sensitive = is_sensitive_scope(paths, text)

  cfg = st.config()
  providers = cfg.get("providers") or {}
  explicit_pool = configured_agents is not None
  agents = configured_agents if configured_agents is not None else (cfg.get("agents") or {})

  effective_wf = _infer_workflow(text, profile, paths, review_round) if wf_req == "auto" else wf_req

  # 1. Two-round review circuit breaker on gate-review.
  if effective_wf == "gate-review" and review_round >= MAX_REVIEW_ROUNDS:
    return _pack(
      "escalate_to_user",
      "gate-review",
      None,
      None,
      None,
      review_gate=True,
      sensitive_scope=sensitive,
      reason=f"Reached {review_round}/{MAX_REVIEW_ROUNDS} review rounds; ask user instead of round 3+ nitpicks.",
    )

  # 2. Break-even inline gate (handoff overhead > small edit / coupled / review-fix cost).
  if effective_wf not in {"gate-review", "dual-plan", "browser-research"}:
    if estimated_lines is not None and estimated_lines <= INLINE_LINE_THRESHOLD:
      return _pack(
        "keep_inline",
        effective_wf,
        None,
        None,
        None,
        sensitive_scope=sensitive,
        reason=f"Small edit ({estimated_lines}<={INLINE_LINE_THRESHOLD} lines); inline beats handoff overhead.",
      )
    if _RE_REVIEW_FIX.search(text):
      return _pack(
        "keep_inline",
        effective_wf,
        None,
        None,
        None,
        sensitive_scope=sensitive,
        reason="Apply review fixes inline to avoid re-explaining context to a worker.",
      )
    exts = _file_exts(paths)
    coupled_files = wf_req == "auto" and bool(exts & _FRONTEND_EXTS) and bool(exts & _BACKEND_EXTS)
    if _RE_TIGHT_COUPLING.search(text) or coupled_files:
      return _pack(
        "keep_inline",
        effective_wf,
        None,
        None,
        None,
        sensitive_scope=sensitive,
        reason="Tight UI<->API coupling needs iterative alignment; keep in parent.",
      )
    is_trivial_profile = bool(
      profile
      and wf_req == "auto"
      and profile["kind"] == "lookup"
      and profile["scope"] == "bounded"
      and profile["risk"] == "low"
      and profile["uncertainty"] == "low"
    )
    if _RE_TRIVIAL.search(text) or is_trivial_profile:
      return _pack(
        "keep_inline",
        effective_wf,
        None,
        None,
        None,
        sensitive_scope=sensitive,
        reason="Trivial lookup/typo fix; keep inline with zero worker startup cost.",
      )

  # 3. Single-owner specialist self-avoidance (caller is already the best owner).
  if effective_wf == "frontend" and host == "claude":
    return _pack(
      "keep_inline", "frontend", None, None, None, sensitive_scope=sensitive,
      reason="Claude already leads frontend/visual work; implement directly inline.",
    )
  if effective_wf == "backend-module" and host == "codex":
    return _pack(
      "keep_inline", "backend-module", None, None, None, sensitive_scope=sensitive,
      reason="Codex already leads isolated backend/algorithm modules; keep inline.",
    )
  if effective_wf == "browser-research" and host == "codex":
    return _pack(
      "keep_inline", "browser-research", None, None, None, sensitive_scope=sensitive,
      reason="Codex already leads multi-step browser/research flows; run inline.",
    )
  if effective_wf == "bulk-cheap" and host == "dsh":
    return _pack(
      "keep_inline", "bulk-cheap", None, None, None, sensitive_scope=sensitive,
      reason="DeepSeek host is already low-cost; run bounded task inline.",
    )

  # 4. Complementary 4-platform routing.
  if effective_wf == "dual-plan":
    if host != "antigravity" and _agent_usable("agy-advisor", agents, providers, explicit_pool):
      role = agents.get("agy-advisor", {})
      prov = providers.get(role.get("provider", "antigravity"), {})
      model = _pick_model(
        ("gemini-4-neon-high", "gemini-pro-orchestrator-neon", "gemini-4-neon-medium"),
        "neon", "antigravity", role, prov, cfg, "gemini-4-neon-high",
      )
      return _pack(
        "dual_opinion", "dual-plan", "agy-advisor", model, "high",
        blind_context=True, sensitive_scope=sensitive,
        reason="Draft parent plan + run blind AGY second opinion from raw intent.",
      )
    if host != "claude" and _agent_usable("claude-reviewer", agents, providers, explicit_pool):
      role = agents.get("claude-reviewer", {})
      prov = providers.get(role.get("provider", "claude"), {})
      model = _pick_model(("opus", "sonnet"), "opus", "claude", role, prov, cfg, "sonnet")
      return _pack(
        "dual_opinion", "dual-plan", "claude-reviewer", model, "high",
        blind_context=True, sensitive_scope=sensitive,
        reason="Run blind Claude architecture second opinion to complement host.",
      )
    role = agents.get("codex-reviewer", {})
    prov = providers.get(role.get("provider", "codex"), {})
    model = _pick_model(("gpt-6.2-sol", "gpt-6.1-sol", "gpt-6-sol"), "sol", "codex", role, prov, cfg, "gpt-6.2-sol")
    return _pack(
      "dual_opinion", "dual-plan", "codex-reviewer", model, "high",
      blind_context=True, sensitive_scope=sensitive,
      reason="Run blind Codex second opinion from raw user intent + code facts.",
    )

  if effective_wf == "copy-ux":
    if host != "antigravity" and _agent_usable("agy-advisor", agents, providers, explicit_pool):
      role = agents.get("agy-advisor", {})
      prov = providers.get(role.get("provider", "antigravity"), {})
      model = _pick_model(
        ("gemini-4-neon-medium", "gemini-4-neon-high"),
        "neon", "antigravity", role, prov, cfg, "gemini-4-neon-medium",
      )
      return _pack(
        "delegate", "copy-ux", "agy-advisor", model, "medium", sensitive_scope=sensitive,
        reason="Polish UI copy/UX flow with Gemini; verify claims against behavior.",
      )
    target = "claude-reviewer" if _agent_usable("claude-reviewer", agents, providers, explicit_pool) else (
      "claude-explorer"
    )
    role = agents.get(target, {})
    prov = providers.get(role.get("provider", "claude"), {})
    model = _pick_model(("sonnet", "opus", "haiku"), "sonnet", "claude", role, prov, cfg, "sonnet")
    return _pack(
      "delegate", "copy-ux", target, model, "medium", sensitive_scope=sensitive,
      reason="Cross-check UI copy and interaction clarity with Claude reviewer.",
    )

  if effective_wf == "frontend":
    if _agent_usable("claude-builder", agents, providers, explicit_pool):
      role = agents.get("claude-builder", {})
      prov = providers.get(role.get("provider", "claude"), {})
      assessed = routing_policy.assess(profile)["complexity"] if profile else "standard"
      pref = ("opus", "sonnet") if assessed == "complex" else ("sonnet", "opus")
      eff = "high" if assessed == "complex" else "medium"
      model = _pick_model(pref, pref[0], "claude", role, prov, cfg, "sonnet")
      return _pack(
        "delegate", "frontend", "claude-builder", model, eff, sensitive_scope=sensitive,
        reason="Delegate standalone visual/frontend module to Claude specialist.",
      )
    role = agents.get("agy-builder", {})
    prov = providers.get(role.get("provider", "antigravity"), {})
    model = _pick_model(
      ("gemini-4-neon-high", "gemini-4-neon-medium"),
      "neon", "antigravity", role, prov, cfg, "gemini-4-neon-high",
    )
    return _pack(
      "delegate", "frontend", "agy-builder", model, "high", sensitive_scope=sensitive,
      reason="Delegate visual/frontend build to AGY builder in isolated worktree.",
    )

  if effective_wf == "gate-review":
    eff = "xhigh" if sensitive else "high"
    if _agent_usable("codex-reviewer", agents, providers, explicit_pool):
      role = agents.get("codex-reviewer", {})
      prov = providers.get(role.get("provider", "codex"), {})
      model = _pick_model(("gpt-6.2-sol", "gpt-6.1-sol", "gpt-6-sol"), "sol", "codex", role, prov, cfg, "gpt-6.2-sol")
      why = (
        "Sensitive scope triggers Sol xhigh 2-tier gate review (max 2 rounds)."
        if sensitive else "Pre-merge Sol high 2-tier gate review (BLOCKING vs ADVISORY, max 2 rounds)."
      )
      return _pack(
        "delegate", "gate-review", "codex-reviewer", model, eff,
        review_gate=True, sensitive_scope=sensitive, reason=why,
      )
    role = agents.get("claude-reviewer", {})
    prov = providers.get(role.get("provider", "claude"), {})
    model = _pick_model(("opus", "sonnet"), "sonnet", "claude", role, prov, cfg, "sonnet")
    return _pack(
      "delegate", "gate-review", "claude-reviewer", model, "high",
      review_gate=True, sensitive_scope=sensitive,
      reason="Fallback pre-merge 2-tier gate review via Claude reviewer.",
    )

  if effective_wf == "bulk-cheap":
    needs_write = bool(output_files) or bool(profile and profile["kind"] in {"mechanical", "implementation"})
    ds_choice = _pick_deepseek_agent(needs_write, agents, providers)
    if ds_choice is not None:
      return _pack(
        "delegate", "bulk-cheap", ds_choice[0], ds_choice[1], "low", sensitive_scope=sensitive,
        reason="Offload bounded mechanical/docs/exploration work to DeepSeek Flash.",
      )
    if not needs_write and _agent_usable("claude-explorer", agents, providers, explicit_pool):
      role = agents.get("claude-explorer", {})
      prov = providers.get(role.get("provider", "claude"), {})
      model = _pick_model(("haiku", "sonnet"), "haiku", "claude", role, prov, cfg, "haiku")
      return _pack(
        "delegate", "bulk-cheap", "claude-explorer", model, "low", sensitive_scope=sensitive,
        reason="Offload bounded read exploration/docs check to cheap Haiku explorer.",
      )
    target = "codex-builder" if needs_write else "codex-reviewer"
    role = agents.get(target, {})
    prov = providers.get(role.get("provider", "codex"), {})
    model = _pick_model(
      ("gpt-6.1-terra", "gpt-5.6-terra", "gpt-6.2-sol"),
      "terra", "codex", role, prov, cfg, "gpt-6.1-terra",
    )
    return _pack(
      "delegate", "bulk-cheap", target, model, "low", sensitive_scope=sensitive,
      reason="Use fast Terra low lane for bounded mechanical/read work.",
    )

  if effective_wf == "browser-research":
    target = "codex-builder" if output_files else "codex-reviewer"
    role = agents.get(target, {})
    prov = providers.get(role.get("provider", "codex"), {})
    model = _pick_model(("gpt-6.2-sol", "gpt-6.1-sol", "gpt-6-sol"), "sol", "codex", role, prov, cfg, "gpt-6.2-sol")
    return _pack(
      "delegate", "browser-research", target, model, "medium", sensitive_scope=sensitive,
      reason="Delegate multi-step browser/cross-site research to Codex Sol.",
    )

  # Default: backend-module (frozen-interface backend, algorithm, data, debugging).
  role = agents.get("codex-builder", {})
  prov = providers.get(role.get("provider", "codex"), {})
  sol_first = (cfg.get("model_policy") or {}).get("mode") == "sol-first"
  if profile is not None:
    assessed_info = routing_policy.assess(profile)
    comp = assessed_info["complexity"]
    rec_eff = assessed_info["recommended_effort"]
  elif _RE_COMPLEX_BACKEND.search(text):
    comp, rec_eff = "complex", "high"
  elif not sensitive and _RE_SIMPLE_BACKEND.search(text):
    comp, rec_eff = "simple", "low"
  else:
    comp, rec_eff = "standard", ("high" if sensitive else "medium")

  if comp == "simple":
    model = _pick_model(("gpt-6.1-terra", "gpt-5.6-terra"), "terra", "codex", role, prov, cfg, "gpt-6.1-terra")
    return _pack(
      "delegate", "backend-module", "codex-builder", model, "low", sensitive_scope=sensitive,
      reason="Delegate bounded low-risk backend module to Codex Terra low.",
    )
  if comp == "complex" and not sol_first:
    model = _pick_model(
      ("gpt-6.2-astra", "gpt-6.1-astra", "gpt-6-astra"),
      "astra", "codex", role, prov, cfg, "gpt-6.2-astra",
    )
    return _pack(
      "delegate", "backend-module", "codex-builder", model, "high", sensitive_scope=sensitive,
      reason="Genuine frontier-complexity module warrants Codex Astra high.",
    )
  model = _pick_model(("gpt-6.2-sol", "gpt-6.1-sol", "gpt-6-sol"), "sol", "codex", role, prov, cfg, "gpt-6.2-sol")
  eff = "high" if (comp == "complex" or sensitive or rec_eff == "high") else "medium"
  why = (
    "sol-first starts complex module on Codex Sol high before Astra escalation."
    if comp == "complex" else "Delegate frozen-interface backend/algorithm module to Codex Sol."
  )
  return _pack(
    "delegate", "backend-module", "codex-builder", model, eff, sensitive_scope=sensitive, reason=why,
  )


def contract_snippet(
  workflow: str = "auto",
  *,
  blind_context: bool = False,
  review_gate: bool = False,
) -> str:
  """Return compact prompt contract snippet, or empty string (0 tokens) by default.

  Args:
    workflow: Workflow name validated against ``WORKFLOWS``.
    blind_context: Inject <=240-char blind independent second-opinion contract.
    review_gate: Inject <=280-char 2-tier ``BLOCKING`` vs ``ADVISORY`` review contract.

  Returns:
    Prompt contract text when ``blind_context`` or ``review_gate`` is True, else ``""``.

  Raises:
    ValueError: If ``workflow`` is invalid or flags are non-boolean.
  """
  normalize_workflow(workflow)
  if type(blind_context) is not bool or type(review_gate) is not bool:
    raise ValueError("blind_context and review_gate must be booleans")
  parts: list[str] = []
  if blind_context:
    parts.append(_BLIND_CONTRACT)
  if review_gate:
    parts.append(_REVIEW_GATE_CONTRACT)
  return "\n".join(parts)
