"""Dispatcher: re-exports for backward compatibility.

worker.py, cli.py, and tests import from this module. All implementations
now live in dispatch.py, agent.py, repo_intel.py, prompts.py, triage.py, review.py.
"""

# Re-exports from dispatch.py (task lifecycle, credential sync, retry logic)
# Re-exports from agent.py
from .agent import run_agent as run_agent  # noqa: F811
from .agent import run_verify as run_verify  # noqa: F811
from .dispatch import dispatch_task as dispatch_task  # noqa: F811
from .dispatch_helpers import _mark_issue_failed as _mark_issue_failed  # noqa: F811
from .dispatch_helpers import _pick_retry_model as _pick_retry_model  # noqa: F811
from .dispatch_helpers import retry_with_ci_context as retry_with_ci_context  # noqa: F811
from .dispatch_helpers import sync_agent_credentials as sync_agent_credentials  # noqa: F811

# Re-exports from git_ops.py
from .git_ops import cleanup_task_artifacts as cleanup_task_artifacts  # noqa: F811
from .git_ops import clone_or_fetch as clone_or_fetch  # noqa: F811
from .git_ops import make_branch_name as make_branch_name  # noqa: F811
from .git_ops import repo_name_from_url as repo_name_from_url  # noqa: F811
from .git_ops import validate_github_url as validate_github_url  # noqa: F811
from .navigation import generate_navigation_context as generate_navigation_context  # noqa: F811

# Re-exports from prompts.py (formerly in agent.py)
from .prompts import AGENT_PROMPT_TEMPLATE as AGENT_PROMPT_TEMPLATE  # noqa: F811

# Re-exports from repo_intel.py (formerly in agent.py)
from .repo_intel import detect_and_store_stack as detect_and_store_stack  # noqa: F811
from .repo_intel import detect_stack as detect_stack  # noqa: F811
from .repo_intel import get_learnings_text as get_learnings_text  # noqa: F811
from .repo_intel import record_learning as record_learning  # noqa: F811

# Re-exports from review.py
from .review import run_review as run_review  # noqa: F811

# Re-exports from triage.py
from .triage import check_task_conflict as check_task_conflict  # noqa: F811
from .triage import orchestrate_batch as orchestrate_batch  # noqa: F811
from .triage import triage_issue as triage_issue  # noqa: F811
