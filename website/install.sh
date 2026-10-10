#!/usr/bin/env bash
# nyairo guided installer for Linux and Windows WSL / Ubuntu.
set -euo pipefail

main() {
  local version=v0.1.0-rc9
  local archive_sha
  local prefix="${HOME:?}/.local/share/nyairo" profile="$HOME/.nyairo" owner=local-owner
  local setup=1 launch=1
  while (($#)); do
    case "$1" in
      --prefix|--profile|--owner)
        (($# >= 2)) || { printf '缺少 %s 的值。\n' "$1" >&2; return 2; }
        case "$1" in --prefix) prefix=$2;; --profile) profile=$2;; --owner) owner=$2;; esac
        shift 2;;
      --no-setup) setup=0; shift;;
      --no-launch) launch=0; shift;;
      -h|--help)
        printf '%s\n' 'nyairo 一条命令引导安装（Linux / Windows WSL）' \
          '默认：下载固定 rc9 → 准备 uv / Python → 安装依赖 → 创建个人配置 → 模型设置 → 聊天。' \
          '--prefix DIR    程序目录，默认 ~/.local/share/nyairo' \
          '--profile DIR   个人数据目录，默认 ~/.nyairo' \
          '--owner ID      本地身份标识，默认 local-owner' \
          '--no-setup      暂不打开模型设置；之后运行 nyairo setup model' \
          '--no-launch     完成安装后暂不启动聊天'
        return 0;;
      *) printf '不认识的参数：%s；用 --help 查看。\n' "$1" >&2; return 2;;
    esac
  done
  [[ $(uname -s) == Linux ]] || { printf '请在 Linux 或 Windows 的 WSL / Ubuntu 终端运行。\n' >&2; return 1; }
  [[ $prefix == /* && $profile == /* && $prefix != / && $profile != / ]] || { printf '程序和数据目录必须是完整绝对路径，且不能是 /。\n' >&2; return 2; }
  [[ $owner =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$ ]] || { printf '身份标识需用 1–128 位英文字母、数字、横线或下划线。\n' >&2; return 2; }
  local tool
  for tool in curl tar sha256sum mktemp flock; do
    command -v "$tool" >/dev/null || { printf '缺少 %s。Ubuntu 可先运行：sudo apt-get update && sudo apt-get install -y curl tar coreutils util-linux\n' "$tool" >&2; return 1; }
  done
  # The script itself can arrive through a pipe. Wizard/chat input must use the terminal.
  if ((setup || launch)); then
    if ! { exec 9<>/dev/tty; } 2>/dev/null; then
      printf '模型设置需要终端。请直接在 Ubuntu / Linux 终端运行；无人值守安装可加 --no-setup --no-launch。\n' >&2
      return 1
    fi
  fi
  umask 077
  [[ ! -L $prefix && ! -L $profile ]] || { printf '程序或个人数据目录是符号链接，请改用独立目录。\n' >&2; return 1; }
  mkdir -p "$prefix" "$HOME/.local/bin"
  prefix=$(cd "$prefix" && pwd -P)
  # uv discovers configuration relative to cwd; do not inherit an unrelated directory.
  cd "$prefix"
  local release="$prefix/releases/$version" lock="$prefix/.install-lock"
  local launcher="$HOME/.local/bin/nyairo" state="$prefix/$version.source-sha256"
  [[ $profile != "$prefix" && $profile != "$prefix/"* ]] || { printf '个人数据目录必须放在程序目录之外。\n' >&2; return 1; }
  if [[ -e $launcher || -L $launcher ]]; then
    [[ -f $launcher && ! -L $launcher && $(sed -n '2p' "$launcher") == '# nyairo managed launcher v1' ]] || {
      printf '%s 已存在且不是 nyairo 创建的入口，不会覆盖它。\n' "$launcher" >&2; return 1;
    }
  fi
  if [[ -e $profile && ! -f $profile/chiyo/config.json ]]; then
    printf '%s 已存在且不是已创建的 nyairo 个人配置，不会写入。请通过 --profile 指定新目录。\n' "$profile" >&2
    return 1
  fi
  mkdir "$lock" 2>/dev/null || { printf '另一个安装正在进行（%s）。确认没有安装进程后再移除此空锁目录。\n' "$lock" >&2; return 1; }
  local temp
  temp=$(mktemp -d "$prefix/.download.XXXXXXXX") || { rmdir -- "$lock"; return 1; }
  # Only these installer-created paths are cleaned; source and personal data are preserved.
  local cleanup
  printf -v cleanup 'rm -rf -- %q; rmdir -- %q 2>/dev/null || true' "$temp" "$lock"
  trap "$cleanup" EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  printf '\n[1/5] 准备安装工具和 Python（首次下载可能需要几分钟）\n'
  local uv
  if command -v uv >/dev/null; then
    uv=$(command -v uv)
  elif [[ -x $HOME/.local/bin/uv ]]; then
    uv="$HOME/.local/bin/uv"
  else
    curl -fsSL --retry 2 --connect-timeout 20 https://astral.sh/uv/install.sh -o "$temp/uv-install.sh"
    UV_INSTALL_DIR="$HOME/.local/bin" UV_NO_MODIFY_PATH=1 sh "$temp/uv-install.sh"
    uv="$HOME/.local/bin/uv"
  fi
  export PATH="$HOME/.local/bin:$PATH"
  "$uv" python install 3.13
  local python
  python=$("$uv" python find 3.13)
  printf '\n[2/5] 下载并核对 nyairo %s\n' "$version"
  local asset="https://github.com/L1AN929/nyairo/releases/download/$version/nyairo-$version.zip"
  curl -fsSL --retry 2 --connect-timeout 20 "$asset.sha256" -o "$temp/source.sha256"
  archive_sha=$(awk 'NR==1 {print $1}' "$temp/source.sha256")
  [[ $archive_sha =~ ^[0-9a-f]{64}$ ]] || { printf '发行校验值无效。\n' >&2; return 1; }
  if [[ -d $release ]]; then
    [[ ! -L $release && -f $state && $(cat "$state") == "$archive_sha" ]] || {
      printf '%s 已存在但没有匹配的安装记录，不会覆盖。请指定新的 --prefix。\n' "$release" >&2; return 1;
    }
  else
    [[ ! -e $release && ! -L $release ]] || { printf '程序路径已被其他文件占用。\n' >&2; return 1; }
    curl -fsSL --retry 2 --connect-timeout 20 "$asset" -o "$temp/source.zip"
    printf '%s  %s\n' "$archive_sha" "$temp/source.zip" | sha256sum -c -
    mkdir "$temp/source"
    # Python's ZIP extractor confines paths to this new private staging directory.
    "$python" -m zipfile -e "$temp/source.zip" "$temp/source"
    mkdir -p "$prefix/releases"
    mv "$temp/source" "$release"
    printf '%s\n' "$archive_sha" > "$state"
  fi
  "$python" "$release/scripts/verify_manifest.py"
  printf '\n[3/5] 安装运行环境\n'
  UV_PYTHON=3.13 "$uv" sync --project "$release/vendor/hermes" --frozen --extra messaging --extra web
  "$release/vendor/hermes/.venv/bin/python" "$release/scripts/verify_manifest.py"
  printf '\n[4/5] 准备个人配置（记忆与生活状态；当前本地账号可使用）\n'
  if [[ -f $profile/chiyo/config.json ]]; then
    printf '保留已有个人配置、人设、密钥与记录：%s\n' "$profile"
  else
    "$release/vendor/hermes/.venv/bin/python" "$release/scripts/setup_profile.py" \
      --home "$profile" --owner "$owner" --memory --life --allow-local-owner
  fi
  HERMES_HOME="$profile" bash "$release/scripts/update.sh" --adopt --releases-dir "$prefix/releases"
  # Idempotent PATH setup makes the launcher available in a newly opened shell.
  local rc="$HOME/.bashrc" path_line='export PATH="$HOME/.local/bin:$PATH" # nyairo launcher'
  [[ ! -L $rc ]] || { printf '.bashrc 是链接，跳过 PATH 写入。入口完整路径：%s\n' "$launcher"; rc=''; }
  if [[ -n $rc ]] && ! grep -Fqx "$path_line" "$rc" 2>/dev/null; then
    printf '\n%s\n' "$path_line" >> "$rc"
  fi
  printf '\n[5/5] 程序安装完成\n个人数据：%s\n以后打开新终端，运行 nyairo 即可继续聊天。\n' "$profile"
  rm -rf -- "$temp"
  rmdir -- "$lock"
  trap - EXIT INT TERM
  if ((setup)); then
    printf '\n接下来选择模型并填写自己的连接信息；密钥由设置向导保存，不会写进安装命令。\n'
    "$launcher" setup model <&9
  else
    printf '模型尚未由安装器设置。准备好后运行：%s setup model\n' "$launcher"
  fi
  if ((launch)); then
    printf '\n进入聊天；退出后用 nyairo 再次打开。\n'
    exec "$launcher" <&9
  fi
}

main "$@"
