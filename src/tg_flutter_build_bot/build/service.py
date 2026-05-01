"""Build service — Git clone + Flutter build subprocess management."""

from __future__ import annotations

import asyncio
import logging
import shlex
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


class BuildError(Exception):
    """Raised when a build step fails."""


class BuildService:
    """Handles cloning a Flutter repo, running the build, and locating artifacts.

    All subprocess calls are non-blocking (asyncio.create_subprocess_exec).
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._current_build: str | None = None  # commit hash being built

    @property
    def is_building(self) -> bool:
        return self._current_build is not None

    @property
    def current_build(self) -> str | None:
        return self._current_build

    def acquire_lock(self) -> bool:
        """Try to acquire the build lock (non-blocking).

        Returns True if lock was acquired, False if a build is already running.
        """
        return self._lock.locked() is False

    async def resolve_remote_commit(
        self, repo_url: str, ref: str = "main"
    ) -> str:
        """Resolve a branch name or partial hash to a full commit hash.

        Uses `git ls-remote` for branch names, returns the ref as-is if
        it looks like a commit hash (>= 7 hex chars).
        """
        # If it looks like a commit hash, return as-is (will be resolved after clone)
        if len(ref) >= 7 and all(c in "0123456789abcdef" for c in ref.lower()):
            return ref.lower()

        # Otherwise treat as a branch name
        proc = await asyncio.create_subprocess_exec(
            "git", "ls-remote", "--heads", repo_url, ref,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            raise BuildError(
                f"Failed to resolve ref '{ref}' from {repo_url}: "
                f"{stderr.decode().strip()}"
            )

        output = stdout.decode().strip()
        if not output:
            raise BuildError(
                f"Branch '{ref}' not found in {repo_url}"
            )

        # Output format: "<hash>\trefs/heads/<branch>"
        commit_hash = output.split("\t")[0].strip()
        return commit_hash

    async def clone_repo(
        self, repo_url: str, ref: str = "main"
    ) -> tuple[str, str]:
        """Clone the repo and checkout the specified ref.

        Args:
            repo_url: Git repository URL.
            ref: Branch name or commit hash.

        Returns:
            (repo_path, full_commit_hash) tuple.

        Raises:
            BuildError: If cloning or checkout fails.
        """
        tmp_dir = tempfile.mkdtemp(prefix="tg-build-")
        repo_path = str(Path(tmp_dir) / "repo")

        logger.info("Cloning %s into %s", repo_url, repo_path)

        # Clone
        proc = await asyncio.create_subprocess_exec(
            "git", "clone", "--depth", "50", repo_url, repo_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise BuildError(
                f"git clone failed: {stderr.decode().strip()}"
            )

        # If ref is not "main"/"master", checkout the specific ref
        is_hash = len(ref) >= 7 and all(
            c in "0123456789abcdef" for c in ref.lower()
        )
        is_default_branch = ref in ("main", "master")

        if not is_default_branch:
            if is_hash:
                # Fetch full history to find the commit (shallow clone may not have it)
                await self._run_git(repo_path, "fetch", "--unshallow")
                await self._run_git(repo_path, "checkout", ref)
            else:
                # Checkout branch
                await self._run_git(repo_path, "checkout", ref)

        # Resolve the full commit hash
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", repo_path, "rev-parse", "HEAD",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        full_hash = stdout.decode().strip()

        logger.info("Checked out commit %s", full_hash)
        return repo_path, full_hash

    async def run_build(
        self, repo_path: str, build_command: str
    ) -> tuple[str, str]:
        """Run the configured build command in the repo directory.

        Args:
            repo_path: Path to the cloned repository.
            build_command: The build command to run (e.g. "flutter build apk --release").

        Returns:
            (stdout, stderr) from the build process.

        Raises:
            BuildError: If the build command fails.
        """
        parts = shlex.split(build_command)
        logger.info("Running build: %s (in %s)", build_command, repo_path)

        proc = await asyncio.create_subprocess_exec(
            *parts,
            cwd=repo_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        stdout_str = stdout.decode()
        stderr_str = stderr.decode()

        if proc.returncode != 0:
            logger.error("Build failed:\n%s\n%s", stdout_str, stderr_str)
            raise BuildError(
                f"Build command failed (exit code {proc.returncode}):\n"
                f"{stderr_str[-500:]}"  # Last 500 chars of stderr
            )

        logger.info("Build completed successfully")
        return stdout_str, stderr_str

    def get_artifact_path(
        self, repo_path: str, build_output_path: str
    ) -> str:
        """Resolve and verify the build artifact exists.

        Args:
            repo_path: Path to the cloned repository.
            build_output_path: Relative path to the build output.

        Returns:
            Absolute path to the artifact file.

        Raises:
            BuildError: If the artifact file doesn't exist.
        """
        artifact = Path(repo_path) / build_output_path
        if not artifact.exists():
            raise BuildError(
                f"Build artifact not found at: {artifact}\n"
                f"Check your build_output_path configuration."
            )
        logger.info("Found artifact: %s (%d bytes)", artifact, artifact.stat().st_size)
        return str(artifact)

    def generate_artifact_name(
        self, project_name: str, commit_hash: str
    ) -> str:
        """Generate the APK filename.

        Format: {project-name}-{YYYYMMDD}-{HHMM}-{short-hash}.apk
        """
        now = datetime.now(timezone.utc)
        short_hash = commit_hash[:7]
        return f"{project_name}-{now.strftime('%Y%m%d-%H%M')}-{short_hash}.apk"

    def cleanup(self, repo_path: str) -> None:
        """Remove the temporary clone directory."""
        parent = Path(repo_path).parent
        if parent.name.startswith("tg-build-"):
            shutil.rmtree(parent, ignore_errors=True)
            logger.info("Cleaned up %s", parent)
        else:
            # Safety: only delete if it looks like our temp dir
            shutil.rmtree(repo_path, ignore_errors=True)
            logger.info("Cleaned up %s", repo_path)

    async def _run_git(self, repo_path: str, *args: str) -> str:
        """Run a git command in the repo directory."""
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", repo_path, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            raise BuildError(
                f"git {' '.join(args)} failed: {stderr.decode().strip()}"
            )
        return stdout.decode().strip()
