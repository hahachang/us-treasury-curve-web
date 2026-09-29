#!/usr/bin/env bash
# YouTube 逐字稿 pipeline — Mac mini 新機設定 / 驗證腳本
# 用法：bash setup_yt_pipeline_mac.sh   （在要放 py/ 的上層目錄執行）
set -u
ok()   { printf "  \033[32m✔\033[0m %s\n" "$*"; }
bad()  { printf "  \033[31m✘\033[0m %s\n" "$*"; FAIL=1; }
hdr()  { printf "\n\033[1m== %s ==\033[0m\n" "$*"; }
FAIL=0

hdr "步驟 1：clone"
[ -d py ] || git clone https://github.com/haha/py.git || bad "git clone 失敗（私有 repo 需先 gh auth login 或設定 SSH key）"
for f in py/CLAUDE.md py/handoffs/handoff-youtube-material-pipeline.md \
         py/skills/youtube-material-pipeline/SKILL.md \
         py/skills/whisper-transcription-reliability/SKILL.md \
         py/skills/local-llm-structured-output/SKILL.md; do
  [ -f "$f" ] && ok "$f" || bad "缺少 $f"
done

hdr "步驟 2：依賴"
if ! command -v brew >/dev/null; then
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  eval "$(/opt/homebrew/bin/brew shellenv)"
fi
command -v brew >/dev/null && ok "brew $(brew --version | head -1)" || bad "Homebrew"

PYV=$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo 0)
python3 -c 'import sys;sys.exit(sys.version_info<(3,11))' 2>/dev/null \
  && ok "python3 $PYV" || { brew install python@3.12 && ok "已安裝 python@3.12（請確認 which python3 指向 /opt/homebrew/bin）"; }

for pkg in uv ffmpeg deno; do
  command -v "$pkg" >/dev/null || brew install "$pkg"
  command -v "$pkg" >/dev/null && ok "$pkg" || bad "$pkg"
done

python3 -m pip install --break-system-packages -U yt-dlp zhconv

if command -v ollama >/dev/null; then
  ollama list | grep -q 'qwen2.5:14b' || ollama pull qwen2.5:14b
else
  bad "Ollama 未安裝：到 https://ollama.com/download 下載 app，開啟後重跑本腳本"
fi

hdr "步驟 3：驗證"
yt-dlp --version >/dev/null 2>&1 && ok "yt-dlp $(yt-dlp --version)" || bad "yt-dlp"
uvx --from mlx-whisper mlx_whisper --help 2>&1 | head -5 && ok "mlx-whisper" || bad "mlx-whisper"
ollama list 2>/dev/null | grep qwen2.5 && ok "qwen2.5" || bad "qwen2.5 模型"
python3 -c "import zhconv; print('ok')" >/dev/null 2>&1 && ok "zhconv" || bad "zhconv"
(cd py && python3 tools/tool_yt_material_pipeline.py --help | head -20) && ok "pipeline --help" || bad "pipeline --help"
N=$(python3 -c "import json;d=json.load(open('py/resources/transcript_mappings/custom_mapping.json'));print(len(d))" 2>/dev/null || echo 0)
[ "$N" -ge 300 ] && ok "custom_mapping.json：$N 筆" || bad "custom_mapping.json 筆數 $N（需 300+）"

hdr "步驟 4：Google Drive 資料夾"
ROOT=$(ls -d "$HOME"/Library/CloudStorage/GoogleDrive-*/我的雲端硬碟 2>/dev/null | head -1)
if [ -n "$ROOT" ]; then
  ok "$ROOT"
  for d in Emmy 郭恭克 逐字稿; do [ -d "$ROOT/$d" ] && ok "  $d" || bad "  找不到 $ROOT/$d"; done
  cat <<EOF

  依序執行（把 URL 換成任一 YouTube 網址）：
  cd py
  python3 tools/tool_yt_material_pipeline.py "URL" --no-whisper --copy-to-drive --drive-dir "$ROOT/Emmy"   --save-drive-profile emmy
  python3 tools/tool_yt_material_pipeline.py "URL" --no-whisper --copy-to-drive --drive-dir "$ROOT/郭恭克" --save-drive-profile guo
  python3 tools/tool_yt_material_pipeline.py "URL" --no-whisper --copy-to-drive --drive-dir "$ROOT/逐字稿" --save-drive-profile other
EOF
else
  bad "找不到 Google Drive 同步資料夾：請安裝 Google Drive 桌面版並等同步完成"
fi

hdr "睡眠設定"
pmset -g | grep -E "^\s*sleep|displaysleep"
pmset -g | awk '$1=="sleep"{exit ($2!=0)}' && ok "系統睡眠已關" \
  || echo "  ⚠ sleep≠0：系統設定→節能 關掉「電腦睡眠」，或長片用 caffeinate -s <指令>"

hdr "步驟 5（手動）"
echo "  Chrome 登入有頻道會員的 YouTube 帳號，然後完整測試："
echo "  cd py && python3 tools/tool_yt_material_pipeline.py \"URL\" --cookies-from-browser chrome --no-whisper --copy-to-drive --drive-profile emmy"

echo; [ $FAIL -eq 0 ] && echo "全部通過 ✅" || echo "有項目未通過 ❌（見上方 ✘）"
