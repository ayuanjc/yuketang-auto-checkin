# 内容安全策略（前端为零构建单页，脚本全部外链，无需 unsafe-inline script）
add_header Content-Security-Policy "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob: https://mp.weixin.qq.com; connect-src 'self'; font-src 'self' data:; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'self'" always;
