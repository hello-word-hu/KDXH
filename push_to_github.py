"""把本仓库推送到 GitHub —— 不需要 Git for Windows，也不需要凭据管理器。

用法：
    python push_to_github.py

会先问你一个 GitHub Personal Access Token（输入时不显示，不会存到任何文件），
然后用 GitHub 的 Git Data API 把当前提交过的文件一次性推上去（产生一个提交）。

为什么不用 git push：
    这台机器上只有 Cherry Studio 自带的 git，没有 Git Credential Manager，
    也没有配置任何凭据助手，git push 无法登录。这个脚本绕开 git，
    直接用 HTTPS 调 GitHub 接口，只用 Python 标准库。

Token 怎么来：
    GitHub → 右上角头像 → Settings → Developer settings
    → Personal access tokens → Tokens (classic) → Generate new token (classic)
    勾选 **repo** 这一项即可，有效期随便（用完可以删掉）。
"""
from __future__ import annotations

import base64
import getpass
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

OWNER = "hello-word-hu"
REPO = "KDXH"
BRANCH = "main"
API = "https://api.github.com"

HERE = os.path.dirname(os.path.abspath(__file__))


def api(method: str, path: str, token: str, payload=None):
    url = API + path
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "push-to-github")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data is not None:
        req.add_header("Content-Type", "application/json; charset=utf-8")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read()
            return r.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            detail = json.loads(body)
        except Exception:
            detail = body.decode("utf-8", "replace")[:300]
        return exc.code, detail


def git(*args) -> str:
    """调 git 取信息；本机没有 git（或不在 PATH）时返回空串，不影响推送。"""
    try:
        out = subprocess.run(["git", *args], cwd=HERE, capture_output=True,
                             timeout=20)
        return out.stdout.decode("utf-8", "replace")
    except Exception:
        return ""


# 万一没有 git，就用这份清单自己走目录（和 .gitignore 保持一致）
SKIP_DIRS = {"libs", ".edge_profile", "__pycache__", ".git", ".idea",
             ".vscode", "site_dump", "edge_profile"}
SKIP_FILES = {"config.json", "config.local.json", "gui_last.json",
              "run_log.txt", ".DS_Store", "Thumbs.db", "desktop.ini"}


def walk_files() -> list:
    """不用 git 也能列出该上传的文件。"""
    out = []
    for root, dirs, names in os.walk(HERE):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for n in names:
            if n in SKIP_FILES or n.endswith((".pyc", ".log")):
                continue
            full = os.path.join(root, n)
            rel = os.path.relpath(full, HERE)
            out.append(rel.replace("\\", "/"))
    return sorted(out)


def read_windows_credential(target: str):
    """从 Windows 凭据管理器里读一条凭据（很多工具会把 GitHub token 存这儿）。

    读不到就返回 None，不报错。
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class CREDENTIAL(ctypes.Structure):
            _fields_ = [
                ("Flags", wintypes.DWORD),
                ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR),
                ("Comment", wintypes.LPWSTR),
                ("LastWritten", wintypes.FILETIME),
                ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
                ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD),
                ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR),
                ("UserName", wintypes.LPWSTR),
            ]

        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        ptr = ctypes.POINTER(CREDENTIAL)()
        ok = advapi.CredReadW(ctypes.c_wchar_p(target), 1, 0, ctypes.byref(ptr))
        if not ok:
            return None
        try:
            blob = ctypes.string_at(ptr.contents.CredentialBlob,
                                    ptr.contents.CredentialBlobSize)
            # 通用凭据里的 blob 有的是 UTF-8/ASCII，有的是 UTF-16LE，都试一下
            for enc in ("utf-8", "utf-16-le"):
                try:
                    txt = blob.decode(enc).strip("\x00").strip()
                except (UnicodeDecodeError, LookupError):
                    continue
                if txt and all(32 <= ord(ch) < 127 for ch in txt):
                    return txt
            return None
        finally:
            advapi.CredFree(ptr)
    except Exception:
        return None


def get_token_interactively() -> str:
    """先看 Windows 凭据管理器里有没有现成的，没有再问用户要。"""
    for target in ("GitHub - https://api.github.com/%s" % OWNER,
                   "git:https://github.com", "GitHub"):
        tok = read_windows_credential(target)
        if tok:
            print("（在 Windows 凭据管理器里找到现成的 GitHub 凭据：%s）" % target)
            return tok
    print()
    return getpass.getpass("请粘贴 GitHub Token（输入时不显示）: ").strip()


def main() -> int:
    print("=" * 62)
    print("推送到 https://github.com/%s/%s" % (OWNER, REPO))
    print("=" * 62)

    # ---- 1. 取要推送的文件 ----
    files = [x for x in git("-c", "core.quotePath=false", "ls-files")
             .splitlines() if x.strip()]
    if files:
        print("（文件清单来自 git ls-files）")
    else:
        files = walk_files()
        print("（本机没有可用的 git，改用目录扫描）")
    if not files:
        print("！没有找到要上传的文件")
        return 1
    msg = git("log", "-1", "--pretty=%s").strip() or "更新抢课脚本"
    author_name = git("config", "user.name").strip() or OWNER
    author_email = git("config", "user.email").strip() or ""

    # 安全检查：绝不上传这些
    forbidden = {"config.json", "gui_last.json"}
    hit = [f for f in files if f in forbidden or f.startswith(("libs/", ".edge_profile/"))]
    if hit:
        print("！拒绝推送：以下文件包含隐私/体积过大，不该上传：")
        for h in hit:
            print("    " + h)
        return 1

    print("\n将上传 %d 个文件：" % len(files))
    for f in files:
        print("   " + f)

    # ---- 2. 要 token ----
    print()
    token = get_token_interactively()
    if not token:
        print("！没有拿到 token")
        return 1

    # ---- 3. 验证 token & 权限 ----
    print("\n[1/5] 验证 token …")
    st, me = api("GET", "/user", token)
    if st != 200:
        print("！token 无效或已过期：", me)
        return 1
    print("    OK，登录身份：%s" % me.get("login"))

    st, repo = api("GET", f"/repos/{OWNER}/{REPO}", token)
    if st != 200:
        print("！访问不到仓库：", repo)
        return 1
    print("    仓库：%s（%s）" % (repo.get("full_name"),
                                "私有" if repo.get("private") else "公开"))

    # ---- 3.5 空仓库要先"点着" ----
    # GitHub 的 Git Data API 在完全空的仓库上不能用（POST /git/blobs 会回
    # 409 "Git Repository is empty."），得先用 Contents API 造一个初始提交。
    st, ref0 = api("GET", f"/repos/{OWNER}/{REPO}/git/ref/heads/{BRANCH}", token)
    if st != 200:
        print("\n[1.5] 仓库还是空的，先创建一个初始提交 …")
        seed = b"# \xe5\x88\x9d\xe5\xa7\x8b\xe5\x8c\x96\n"
        st, res = api("PUT", f"/repos/{OWNER}/{REPO}/contents/.gitignore",
                      token, {
                          "message": "chore: 初始化仓库",
                          "content": base64.b64encode(seed).decode("ascii"),
                          "branch": BRANCH,
                      })
        if st not in (200, 201):
            print("！初始化失败：", res)
            return 1
        print("    完成，接下来把全部文件作为一个提交推上去")

    # ---- 4. 上传每个文件为 blob ----
    print("\n[2/5] 上传文件内容 …")
    tree_items = []
    for i, rel in enumerate(files, 1):
        full = os.path.join(HERE, rel)
        with open(full, "rb") as fh:
            content = fh.read()
        st, blob = api("POST", f"/repos/{OWNER}/{REPO}/git/blobs", token, {
            "content": base64.b64encode(content).decode("ascii"),
            "encoding": "base64",
        })
        if st not in (200, 201):
            print("！上传 %s 失败：%s" % (rel, blob))
            return 1
        # GitHub 的 tree path 用正斜杠
        tree_items.append({"path": rel.replace("\\", "/"),
                           "mode": "100644", "type": "blob",
                           "sha": blob["sha"]})
        print("    [%d/%d] %s" % (i, len(files), rel))

    # ---- 5. tree -> commit -> ref ----
    print("\n[3/5] 生成目录树 …")
    st, tree = api("POST", f"/repos/{OWNER}/{REPO}/git/trees", token,
                   {"tree": tree_items})
    if st not in (200, 201):
        print("！生成 tree 失败：", tree)
        return 1

    print("[4/5] 生成提交 …")
    parents = []
    st, ref = api("GET", f"/repos/{OWNER}/{REPO}/git/ref/heads/{BRANCH}", token)
    if st == 200 and ref:
        parents = [ref["object"]["sha"]]
        print("    已有分支 %s，本次是新增提交" % BRANCH)
    else:
        print("    仓库还是空的，本次是首个提交")

    commit_body = {"message": msg, "tree": tree["sha"]}
    if parents:
        commit_body["parents"] = parents
    if author_email:
        commit_body["author"] = {"name": author_name, "email": author_email}
        commit_body["committer"] = {"name": author_name, "email": author_email}
    st, commit = api("POST", f"/repos/{OWNER}/{REPO}/git/commits", token,
                     commit_body)
    if st not in (200, 201):
        print("！生成 commit 失败：", commit)
        return 1

    print("[5/5] 更新分支 …")
    if parents:
        st, out = api("PATCH", f"/repos/{OWNER}/{REPO}/git/refs/heads/{BRANCH}",
                      token, {"sha": commit["sha"], "force": False})
    else:
        st, out = api("POST", f"/repos/{OWNER}/{REPO}/git/refs", token,
                      {"ref": "refs/heads/" + BRANCH, "sha": commit["sha"]})
    if st not in (200, 201):
        print("！更新分支失败：", out)
        return 1

    # ---- 6. 顺手把仓库页面的 About 描述写上免责警示 ----
    print("\n[附加] 更新仓库页面描述（About 栏）…")
    desc = ("⚠️ 请勿用于恶意抢课盈利/黄牛行为，后果自负 · "
            "矿大徐海抢课脚本，含使用说明，建议自行下载观看")
    st, _ = api("PATCH", f"/repos/{OWNER}/{REPO}", token, {"description": desc})
    if st == 200:
        print("    OK")
    else:
        print("    （跳过，不影响代码已经推送成功）")

    print("\n" + "=" * 62)
    print("推送成功！")
    print("   https://github.com/%s/%s" % (OWNER, REPO))
    print("   提交 %s" % commit["sha"][:10])
    print("=" * 62)
    print("\n刚才那个 token 已经用完，可以去 GitHub 设置里把它删掉了。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已取消")
        sys.exit(130)
