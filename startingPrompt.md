You are an expert systems engineer and developer tools architect. I want you to help me build a local-first, open-source agentic build and CI/CD system called **AgentMachinist**. 

AgentMachinist enables a solo developer working on a Mac to bridge GitHub issues with local coding harnesses (like Claude Code, OpenCode, PI, or Codex). 

Please review the core architecture below and help me implement the initial project structure, configuration files, and automation scripts.

---

### Core Architecture & Workflow Requirements

1. **The Harness Abstraction Layer (`machinist.yaml`)**
   - Create a configuration schema and parser that defines which coding harness to use (`claude-code`, `opencode`, `pi`, `codex`), timeout settings, and workspace rules.

2. **Phase 1: GitHub Issue Ingestion & Spec Generation**
   - Write a script or GitHub Action workflow template that triggers upon GitHub issue creation (or when an `agent-task` label is applied).
   - The script should invoke the configured harness to read the issue and generate an implementation spec and plan saved to a dedicated directory (e.g., `.machinist/specs/issue-<number>-spec.md`).
   - It must automatically create a new git branch (`agent/issue-<number>`), commit the spec file, and **open a Draft Pull Request** on GitHub referencing the original issue.

3. **Phase 2: Human Review & Approval Mechanism**
   - Define how approvals are tracked (e.g., transitioning the Draft PR to "Ready for Review" or detecting a specific label/comment like `/machinist-execute`).

4. **Phase 3: Local Execution Daemon / CLI (`machinist`)**
   - Build a lightweight local command-line interface (written in Python, Node, or Go—let's discuss or use a clean Python/Click or Go structure) that can run on my Mac.
   - The CLI should have commands like:
     - `machinist init`: Sets up `machinist.yaml` and local directories.
     - `machinist watch`: Polls or listens for approved PRs/labels on GitHub.
     - `machinist run <issue-number>`: Pulls the approved spec branch locally, invokes the local coding harness (e.g., executing Claude Code or the chosen CLI tool headlessly or interactively with the spec), runs tests, and pushes the implementation commits back to the PR branch.

---

### Your Immediate Task
1. Propose the optimal technology stack (e.g., Python or TypeScript) for the local CLI and GitHub integration.
2. Outline the directory structure for the AgentMachinist repository.
3. Write the initial code for `machinist.yaml` schema, the core CLI entrypoint, and the GitHub API wrapper for handling Draft PR creation.

Let's start step-by-step. Give me the project layout and the core bootstrapping code first.

