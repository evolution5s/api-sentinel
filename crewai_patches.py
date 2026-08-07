"""Narrow, defensive patches for confirmed crewai bugs that break this repo
on Claude Sonnet 5 (and the wider Claude 4.6+ model family). Each patch only
touches what it names explicitly and is applied defensively - if crewai's
internals ever change shape, apply_patches() just skips that one patch and
prints a warning instead of crashing the whole app on import.
"""


def _patch_max_iterations_final_answer_role() -> bool:
    """Root cause (verified directly in crewai.utilities.agent_utils, both
    the installed 1.15.9 and the pinned 1.15.11): when an agent reaches
    max_iter, crewai tries to force a final answer by appending the forcing
    instruction as an ASSISTANT-role message and sending that as the LAST
    message in the conversation. Every current Claude model rejects that
    with a 400 ("This model does not support assistant message prefill.
    The conversation must end with a user message.") - so reaching max_iter
    doesn't gracefully truncate the task, it crashes it, and the exception
    propagates all the way up through crew.kickoff() and takes the rest of
    the cron cycle down with it. Reproduced in production.

    Same behavior, minimal fix: append the forcing instruction as a
    user-role message instead - the one change needed to make the request
    valid on every current Claude model. Not an upstream fix (that belongs
    in crewai itself, worth reporting), just enough to stop max_iter from
    being load-bearing-fatal here.
    """
    try:
        from crewai.agents.parser import AgentFinish
        from crewai.utilities import agent_utils
        from crewai.utilities.agent_utils import format_answer, format_message_for_llm
    except ImportError as exc:
        print(f"[crewai_patches] could not import max_iter patch targets: {exc}")
        return False

    def _fixed_handle_max_iterations_exceeded(formatted_answer, printer, messages, llm, callbacks, verbose=True):
        if verbose:
            printer.print(
                content="Maximum iterations reached. Requesting final answer.",
                color="yellow",
            )

        if formatted_answer and hasattr(formatted_answer, "text"):
            forcing_message = (
                formatted_answer.text + f"\n{agent_utils.I18N_DEFAULT.errors('force_final_answer')}"
            )
        else:
            forcing_message = agent_utils.I18N_DEFAULT.errors("force_final_answer")

        # The one substantive change vs. crewai's own implementation:
        # "user" instead of "assistant" - see the docstring above.
        messages.append(format_message_for_llm(forcing_message, role="user"))

        answer = llm.call(messages, callbacks=callbacks)
        if answer is None or answer == "":
            raise ValueError("Invalid response from LLM call - None or empty.")

        formatted = format_answer(answer=answer)
        if isinstance(formatted, AgentFinish):
            return formatted
        return AgentFinish(thought=formatted.thought, output=formatted.text, text=formatted.text)

    patched_modules = []
    for module_path in ("crewai.agents.crew_agent_executor", "crewai.experimental.agent_executor"):
        try:
            module = __import__(module_path, fromlist=["handle_max_iterations_exceeded"])
        except ImportError:
            continue
        if not hasattr(module, "handle_max_iterations_exceeded"):
            continue
        module.handle_max_iterations_exceeded = _fixed_handle_max_iterations_exceeded
        patched_modules.append(module_path)

    if patched_modules:
        print(f"[crewai_patches] patched handle_max_iterations_exceeded in: {patched_modules}")
    else:
        print(
            "[crewai_patches] WARNING: handle_max_iterations_exceeded not found in any "
            "expected module - patch not applied, max_iter crashes may recur"
        )
    return bool(patched_modules)


def apply_patches() -> None:
    """Apply every patch in this module. Called once, at import time, from
    crew.py - before any Agent/Task is constructed, since the patch has to
    be in place before an agent could ever reach its max_iter cap.
    """
    _patch_max_iterations_final_answer_role()
