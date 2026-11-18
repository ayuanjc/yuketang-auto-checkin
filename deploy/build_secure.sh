#!/usr/bin/env bash
# 字节码发行：源码留 /opt/yktsign-src(0700)，线上只放 -OO .pyc，不落 .py
# 用法: build_secure.sh            编译并切换（幂等，可反复执行）
#       build_secure.sh --restore  回滚：把源码拷回 /opt/yktsign 并删掉 .pyc
set -euo pipefail
LIVE=/opt/yktsign
SRC=/opt/yktsign-src
MODS="alert secure server loc codepool ai svcclient appsession biz acct qrlogin app svc web lesson_watch"

mkdir -p "$SRC"; chmod 700 "$SRC"

if [ "${1:-}" = "--restore" ]; then
    for m in $MODS; do
        [ -f "$SRC/$m.py" ] && cp -f "$SRC/$m.py" "$LIVE/$m.py" && rm -f "$LIVE/$m.pyc"
    done
    echo "restored sources from $SRC"
    exit 0
fi

# 1) 现网 .py 归集进源码库（已编译过则沿用库里的较新源码）
for m in $MODS; do
    if [ -f "$LIVE/$m.py" ]; then cp -f "$LIVE/$m.py" "$SRC/$m.py"; fi
done
chmod 600 "$SRC"/*.py

# 2) -OO 编译（去 docstring / assert），生成 legacy 布局 .pyc
cd "$SRC"
python3 -OO -m compileall -b -q . >/dev/null

# 3) 先落到临时目录，逐个替换，避免半成品上线
STAGE=$(mktemp -d)
for m in $MODS; do cp -f "$SRC/$m.pyc" "$STAGE/$m.pyc"; done
chmod 600 "$STAGE"/*.pyc

for m in $MODS; do
    [ -f "$STAGE/$m.pyc" ] || { echo "missing pyc: $m"; exit 1; }
    mv -f "$STAGE/$m.pyc" "$LIVE/$m.pyc"
    rm -f "$LIVE/$m.py"
    rm -rf "$LIVE/__pycache__/$m".*.pyc 2>/dev/null || true
done
rmdir "$STAGE" 2>/dev/null || true
find "$LIVE" -maxdepth 1 -name '*.py' -delete
find "$LIVE" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true

# 4) 校验可加载
cd "$LIVE"
python3 -OO -c "import sys;sys.path.insert(0,'$LIVE');import secure,biz,app,svcclient;print('imports ok')"
# 5) 生成完整性清单（启动时校验，字节被改即拒绝运行）
cd "$LIVE"
: > /opt/yktsign-build/manifest.sha256
for m in $MODS; do sha256sum "$m.pyc" >> /opt/yktsign-build/manifest.sha256; done
chmod 600 /opt/yktsign-build/manifest.sha256
echo "bytecode deployed: $(ls -1 "$LIVE"/*.pyc | wc -l) modules"
