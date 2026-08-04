---
name: os-news-git-publish
description: Publish os-news-tracker to Gongfeng (kierankfli) then GitHub (hhhhlkf), updating the current feature branch and main on both remotes. Use when the user asks to 推送, publish, sync remotes, update main, push Gongfeng/GitHub, or says to follow the usual dual-remote publish flow.
---

# OS News Dual Remote Publish

Repo: `/data/workspace/os-news-tracker`

| Remote | Name | Account | Identity |
|--------|------|---------|----------|
| `gongfeng` | 工蜂 | `kierankfli` | `kierankfli <kierankfli@tencent.com>` |
| `origin` | GitHub | `hhhhlkf` | `hhhhlkf <153648356@qq.com>` |

Script:

```bash
python3 /root/.codex/skills/os-news-git-publish/scripts/publish.py
```

## Default flow (do this unless user says otherwise)

When the user says to push / publish / 推送 without extra constraints:

1. Inspect `git status` and commit pending relevant changes if needed.
   - Prefer `kierankfli` identity for the local commit when Gongfeng is first.
   - Do **not** commit unrelated junk (e.g. `.cursor/`, random `docs/img/` dumps) unless asked.
2. **Gongfeng first**
   - Push current branch to `gongfeng`
   - Update `gongfeng/main` from current HEAD
3. **GitHub second**
   - Sync current tree to `origin/<current-branch>` as `hhhhlkf`
   - Update `origin/main` from current HEAD as `hhhhlkf`

Concrete commands (current branch example: `feature/site-discovery-agent`):

```bash
# 1) Gongfeng: current branch
python3 /root/.codex/skills/os-news-git-publish/scripts/publish.py push-current \
  --remote gongfeng \
  --branch "$(git -C /data/workspace/os-news-tracker branch --show-current)"

# 2) Gongfeng: main
python3 /root/.codex/skills/os-news-git-publish/scripts/publish.py merge-to \
  --remote gongfeng \
  --target-branch main \
  --source HEAD \
  -m "sync: update main"

# 3) GitHub: current branch (identity-clean tip)
python3 /root/.codex/skills/os-news-git-publish/scripts/publish.py sync-tree \
  --remote github \
  --branch "$(git -C /data/workspace/os-news-tracker branch --show-current)" \
  --source HEAD \
  -m "sync: update feature branch"

# 4) GitHub: main
python3 /root/.codex/skills/os-news-git-publish/scripts/publish.py merge-to \
  --remote github \
  --target-branch main \
  --source HEAD \
  -m "sync: update main"
```

If there are uncommitted changes, commit first with Gongfeng identity, then run the four steps:

```bash
cd /data/workspace/os-news-tracker
# stage only relevant files, then:
GIT_AUTHOR_NAME='kierankfli' GIT_AUTHOR_EMAIL='kierankfli@tencent.com' \
GIT_COMMITTER_NAME='kierankfli' GIT_COMMITTER_EMAIL='kierankfli@tencent.com' \
git commit -m "your message"
```

Or:

```bash
python3 /root/.codex/skills/os-news-git-publish/scripts/publish.py commit-push \
  --remote gongfeng \
  --branch "$(git branch --show-current)" \
  -m "your message"
```

then continue from Gongfeng `merge-to main`.

## Why this order / these commands

- Gongfeng rejects commits authored/committed by `hhhhlkf`; push Gongfeng with `kierankfli`.
- GitHub tip for sync operations should use `hhhhlkf`; `sync-tree` / `merge-to` create an identity-clean tip commit.
- `merge-to` for both remotes uses identity-clean tree sync onto `main`.
- Do not force-push unless the user explicitly asks.

## After publish

Report the four tips briefly:

- `gongfeng/<branch>`
- `gongfeng/main`
- `origin/<branch>`
- `origin/main`
