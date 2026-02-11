#!/usr/bin/env bash
set -euo pipefail

TASK="${1:-Fix TODOs and improve code quality in current repo}"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"

# 실수 방지: main/master 직접 푸시 차단(원하면 제거)
if [[ "$BRANCH" == "main" || "$BRANCH" == "master" ]]; then
  echo "❌ main/master 직접 push는 차단되어 있습니다. feature 브랜치에서 실행하세요."
  exit 1
fi

# 1) Codex로 작업 수행 (수정 허용)
codex exec --full-auto "$TASK"

# 2) 변경사항 있으면 commit + push
if [[ -n "$(git status --porcelain)" ]]; then
  git add -A

  # 커밋 메시지도 Codex가 생성 (1줄만)
  MSG="$(codex exec --ephemeral --sandbox read-only --ask-for-approval never \
    "Write ONE concise Conventional Commit subject for current staged diff. Output one line only, <=72 chars.")"
  MSG="$(echo "$MSG" | head -n1 | tr -d '\r')"

  git commit -m "${MSG:-chore: update files via codex}"
  git push origin "$BRANCH"
  echo "✅ committed & pushed to $BRANCH"
else
  echo "ℹ️ 변경사항 없음"
fi