"""Explicit model exclusions and escalation authorization, independent of model preferences."""
from typing import Any
from fnmatch import fnmatchcase

import storage as st
import routing_policy


def validate(value: dict[str, Any]) -> None:
  """Validate opt-in policy without silently changing existing account permissions."""
  if not isinstance(value, dict) or set(value) - {'mode', 'deny_models', 'quality_escalation'}:
    raise ValueError('model_policy accepts mode, deny_models, quality_escalation')
  if value.get('mode', 'capability-first') not in {'capability-first', 'sol-first'}:
    raise ValueError('model_policy.mode must be capability-first or sol-first')
  patterns = value.get('deny_models', [])
  if not isinstance(patterns, list) or len(patterns) > 64 or any(
      not isinstance(p, str) or not p.strip() or len(p) > 160 for p in patterns):
    raise ValueError('deny_models must contain at most64 nonempty model IDs/globs of at most160 characters')
  if type(value.get('quality_escalation', True)) is not bool:
    raise ValueError('quality_escalation must be boolean')


def allow_xhigh_sol(
  model: str,
  *,
  review_gate: bool = False,
  sensitive_scope: bool = False,
) -> bool:
  """Return True when ``sol`` is authorized for ``xhigh`` sensitive-scope gate review.

  Args:
    model: Candidate or requested model identifier.
    review_gate: Whether 2-tier gate review mode is active.
    sensitive_scope: Whether target paths/task touch sensitive scope.

  Returns:
    True when ``model`` belongs to ``sol`` and review/sensitive scope requests ``xhigh``.
  """
  return routing_policy.family(model) == 'sol' and bool(review_gate or sensitive_scope)


def reason(
  model: str,
  role: dict[str, Any],
  provider: dict[str, Any],
  escalate: bool = False,
  previous: dict[str, Any] | None = None,
  cfg: dict[str, Any] | None = None,
  complexity: str = "",
  *,
  effort: str = "",
  review_gate: bool = False,
  sensitive_scope: bool = False,
) -> str:
  """Explain a hard rejection; explicit choices and retries cannot override deny patterns.

  Args:
    model: Candidate or requested model identifier.
    role: Role configuration dictionary.
    provider: Provider configuration dictionary.
    escalate: Whether explicit escalation was requested.
    previous: Optional prior job record on the same work item.
    cfg: Optional configuration override dictionary.
    complexity: Resolved subtask complexity level, if known.
    effort: Optional requested reasoning effort level.
    review_gate: Whether pre-merge gate review mode is active.
    sensitive_scope: Whether sensitive scope was detected.

  Returns:
    Rejection reason string, or empty string when permitted.
  """
  cfg = st.config() if cfg is None else cfg
  policy = cfg.get('model_policy', {})
  patterns = [*policy.get('deny_models', []), *provider.get('deny_models', []), *role.get('deny_models', [])]
  if any(fnmatchcase(model, pattern) for pattern in patterns):
    return 'Model is forbidden by deny_models: ' + model
  if effort == 'xhigh' and allow_xhigh_sol(model, review_gate=review_gate, sensitive_scope=sensitive_scope):
    return ''
  if policy.get('mode') == 'sol-first' and routing_policy.family(model) == 'astra':
    quality_failure = (policy.get('quality_escalation', True) and previous
      and previous.get('status') == 'completed' and not previous.get('error')
      and routing_policy.family(previous.get('model', '')) == 'sol' and previous.get('review') == 'needs-work'
      and bool(previous.get('review_evidence')))
    if not escalate and not quality_failure:
      return (
        'sol-first requires escalate=true (only for complex tasks) or an evidenced Sol needs-work review '
        'before Astra; split/replan or use Sol to conserve Codex quota'
      )
    if not quality_failure and complexity in {'trivial', 'simple', 'standard'}:
      return (
        'sol-first reserves Codex Astra strictly for complex tasks (or evidenced Sol needs-work escalation); '
        'use Sol (medium/high effort) or Terra for non-complex tasks'
      )
  return ''


def check_job(job: dict[str, Any]) -> None:
  """Recheck current exclusions at the final inference boundary, after slow workspace preparation."""
  cfg = st.config()
  prior_id = job.get('quality_escalation_from')
  previous = None
  if prior_id and cfg.get('model_policy', {}).get('mode') == 'sol-first' and not job.get('escalate', False):
    try:
      previous = st.job(prior_id)
    except ValueError:
      pass  # Missing prior evidence cannot authorize escalation.
  if previous:
    from optimizer import matches_work_item
    if previous.get('session_id') != job.get('session_id') or not matches_work_item(previous, job.get('work_item', 'main')):
      previous = None
  rejection = reason(
    job.get('model', ''),
    cfg['agents'].get(job.get('agent', ''), job.get('role', {})),
    cfg['providers'].get(job.get('provider_name', ''), job.get('provider', {})),
    job.get('escalate', False),
    previous,
    cfg,
    job.get('complexity', ''),
  )
  if rejection:
    raise ValueError(rejection)
