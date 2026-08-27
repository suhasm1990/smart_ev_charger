import html
import os
import re
import subprocess
import threading
from reporting.logger import log
from reporting.notifications import notify
from agent import llm_client

# ── Autonomous Developer Tools ───────────────────────────────────────────────

def dev_read_file(file_path: str, start_line: int = 1, end_line: int = 250) -> str:
    """Reads lines from a file in the repository.
    
    Args:
        file_path: Relative path to the file from repository root (e.g. 'main.py' or 'core/config.py').
        start_line: 1-indexed starting line number (default 1).
        end_line: 1-indexed ending line number (default 250).
    """
    try:
        abs_path = os.path.abspath(file_path)
        if not os.path.exists(abs_path):
            return f"Error: File not found: {file_path}"
        with open(abs_path, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
        selected = lines[max(0, start_line - 1):end_line]
        numbered = [f"{i+start_line}: {line}" for i, line in enumerate(selected)]
        return "".join(numbered) or "(Empty file range)"
    except Exception as e:
        return f"Error reading file {file_path}: {e}"

def dev_write_file(file_path: str, content: str) -> str:
    """Writes or updates a file with the given content.
    
    Args:
        file_path: Relative path to the file.
        content: The complete content to write into the file.
    """
    try:
        abs_path = os.path.abspath(file_path)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, 'w', encoding='utf-8') as f:
            f.write(content)
        return f"Successfully wrote {len(content)} bytes to {file_path}"
    except Exception as e:
        return f"Error writing file {file_path}: {e}"

def dev_list_files(directory: str = ".") -> str:
    """Lists code files in the repository.
    
    Args:
        directory: Directory path to list (default '.').
    """
    try:
        items = []
        for root, dirs, files in os.walk(directory):
            dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ('__pycache__', 'node_modules')]
            for f in files:
                if not f.startswith('.'):
                    rel = os.path.relpath(os.path.join(root, f), directory)
                    items.append(rel)
        return "\n".join(sorted(items)[:100]) or "No files found."
    except Exception as e:
        return f"Error listing directory {directory}: {e}"

def dev_search_code(pattern: str, directory: str = ".") -> str:
    """Searches for a keyword or regex pattern across codebase files.
    
    Args:
        pattern: Text string or regex to search for.
        directory: Directory to search in (default '.').
    """
    matches = []
    try:
        regex = re.compile(pattern, re.IGNORECASE)
        for root, dirs, files in os.walk(directory):
            dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ('__pycache__', 'node_modules')]
            for f in files:
                if f.endswith(('.py', '.json', '.sh', '.md', '.txt', 'Dockerfile', '.yml', '.yaml')):
                    path = os.path.join(root, f)
                    try:
                        with open(path, 'r', encoding='utf-8', errors='replace') as fp:
                            for idx, line in enumerate(fp, 1):
                                if regex.search(line):
                                    matches.append(f"{path}:{idx}: {line.strip()}")
                                    if len(matches) >= 30:
                                        return "\n".join(matches)
                    except Exception:
                        pass
        return "\n".join(matches) or f"No matches found for '{pattern}'"
    except Exception as e:
        return f"Error searching code: {e}"

def dev_run_command(command: str) -> str:
    """Executes a shell command (such as pytest, git, or gh CLI) in the workspace.
    
    Args:
        command: Exact shell command line to run (e.g. 'git status', 'python -m unittest', 'gh pr create').
    """
    try:
        env = dict(os.environ)
        if "GITHUB_TOKEN" in env and not env.get("GH_TOKEN"):
            env["GH_TOKEN"] = env["GITHUB_TOKEN"]

        proc = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            cwd=os.getcwd(),
            timeout=120,
            env=env
        )
        out = (proc.stdout + "\n" + proc.stderr).strip()
        return f"[Exit code {proc.returncode}]\n{out}" if out else f"[Exit code {proc.returncode}] (No output)"
    except subprocess.TimeoutExpired:
        return "Error: Command timed out after 120 seconds."
    except Exception as e:
        return f"Error executing command: {e}"

DEV_TOOLS = [
    dev_read_file,
    dev_write_file,
    dev_list_files,
    dev_search_code,
    dev_run_command
]

DEV_SYSTEM_INSTRUCTION = (
    "You are an expert autonomous software engineer and AI assistant for this repository.\n"
    "Your objective is to diagnose issues/questions, and either implement code fixes with a GitHub Pull Request or provide a clear explanation if no code changes are needed.\n\n"
    "Guidelines:\n"
    "1. Inspect relevant files using 'dev_read_file' or find keywords using 'dev_search_code'.\n"
    "2. If code or documentation changes are made:\n"
    "   a. Modify the file(s) using 'dev_write_file'.\n"
    "   b. Run tests using 'dev_run_command' if applicable.\n"
    "   c. MANDATORY: You MUST run git commands using 'dev_run_command' to commit and open a PR:\n"
    "      git checkout -b fix/<short-issue-name>\n"
    "      git add -A\n"
    "      git commit -m 'fix: <concise message>'\n"
    "      git push -u origin fix/<short-issue-name>\n"
    "      gh pr create --title '<Title>' --body '<Summary of fix>'\n"
    "   d. Output the created GitHub Pull Request URL and a summary of what was changed.\n"
    "3. If NO code change is needed (e.g. the configuration is already correct, or it's an operational/environment issue):\n"
    "   Provide a clear, helpful, and concise explanation directly without creating an unnecessary PR."
)

def load_custom_project_rules() -> str:
    """Reads custom agent rules from .agents/AGENTS.md, AGENTS.md, or .agents/rules/ if present."""
    rules = []
    rule_paths = [".agents/AGENTS.md", "AGENTS.md", "GEMINI.md"]
    for path in rule_paths:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    rules.append(f"\n--- Project Rules ({path}) ---\n" + f.read().strip())
            except Exception:
                pass
    return "\n".join(rules)

def run_dev_agent_loop(task_description: str, pr_number: int = None) -> str:
    """Executes the autonomous agentic developer loop."""
    custom_rules = load_custom_project_rules()
    system_instruction = DEV_SYSTEM_INSTRUCTION
    if custom_rules:
        system_instruction += f"\n\nProject Guidelines & Rules:\n{custom_rules}"

    if pr_number:
        user_prompt = (
            f"Update GitHub PR #{pr_number} with these requested changes:\n"
            f"{task_description}\n\n"
            f"1. Run 'gh pr checkout {pr_number}'.\n"
            f"2. Apply the required modifications using dev_write_file.\n"
            f"3. Run tests.\n"
            f"4. Commit and push updates using 'git push'.\n"
            f"5. Output the PR link and a summary."
        )
    else:
        user_prompt = (
            f"Investigate and resolve the following issue / feature request:\n"
            f"{task_description}\n\n"
            f"Workflow:\n"
            f"1. Inspect relevant files using dev_read_file or dev_search_code.\n"
            f"2. Apply necessary modifications using dev_write_file.\n"
            f"3. When files are modified, you MUST run dev_run_command with:\n"
            f"   git checkout -b <branch_name>\n"
            f"   git add -A\n"
            f"   git commit -m '<commit message>'\n"
            f"   git push -u origin <branch_name>\n"
            f"   gh pr create --title '<Title>' --body '<Summary>'\n"
            f"4. If no code changes are required: provide a clear explanation of your findings."
        )

    log.info(f"DEV AGENT | Starting autonomous loop for task: {task_description[:80]}...")
    
    # Run single multi-turn autonomous tool interaction loop
    response_text, _ = llm_client.chat_with_tools(
        history=[],
        user_text=user_prompt,
        tools=DEV_TOOLS,
        system_instruction=system_instruction,
        max_iterations=15
    )
    return response_text or "Task completed."

def dispatch_dev_task_background(task_description: str, pr_number: int = None):
    """Launches the dev agent in a background thread and sends status to Telegram."""
    def worker():
        notify("⚙️ <b>Autonomous Dev Agent Started</b>\nInvestigating codebase and logs to prepare changes...")
        try:
            import shutil
            if shutil.which("agy"):
                prompt = (
                    f"Checkout PR #{pr_number} and implement: {task_description}"
                    if pr_number
                    else f"Investigate, fix, run tests, and open a PR: {task_description}"
                )
                cmd = ["agy", "-p", prompt, "--dangerously-skip-permissions", "--mode", "accept-edits", "--print-timeout", "15m"]
                proc = subprocess.run(cmd, capture_output=True, text=True, cwd=os.getcwd(), env=os.environ)
                if proc.returncode == 0 and proc.stdout.strip():
                    summary = proc.stdout.strip()
                    notify(f"✅ <b>Dev Agent Finished</b>\n\n<pre>{html.escape(summary[-2500:])}</pre>")
                    return

            summary = run_dev_agent_loop(task_description, pr_number)
            notify(f"✅ <b>Dev Agent Finished</b>\n\n<pre>{html.escape(summary[-2500:])}</pre>")
        except Exception as e:
            log.error(f"Dev agent error: {e}", exc_info=True)
            notify(f"❌ <b>Dev Agent Error</b>\n{html.escape(str(e))}")

    threading.Thread(target=worker, daemon=True).start()
