# -*- coding: utf-8 -*-
"""
ts 分片下载与合并 · Agent 模式版
（解析 m3u8 链接文件 → 逐集下载 ts 分片（AES 解密/断点续传/失败镜像兜底）→ 顺序合并 → ffmpeg 无损转 mp4）
说明：这是脱敏后的学习参考代码。代码里用到的站点 Referer 一律抽象化，
     适配时改成你目标站点的真实值即可。
用法（手动在命令行/编辑器终端里运行；m3u8 链接有时效，抓完尽快下载）：
  python ts_merge.py
  自动读取同目录 m3u8链接.txt（main.py 抓取完成会自动调起本脚本，也可单独运行）
铁律：单实例下载锁（原子 O_EXCL 抢锁）防双下载器双写同一集文件；成品已存在的集自动跳过。
"""
import os
import re
import sys
import time
import shutil
import subprocess
import requests
from urllib.parse import urljoin
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64 x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36 Edg/149.0.0.0",
    "Referer": "https://target-site.example.com/",}   # ← 请改成你的目标站点 Referer
BASE = os.path.dirname(os.path.abspath(__file__))
M3U8_TXT = os.path.join(BASE, 'm3u8链接.txt')
SEG_DIR = os.path.join(BASE, 'ts片段')          # 每集一个子文件夹：ts片段/第N集/

# ===== 单实例下载锁（独立于 main.py 的运行锁——main.py 调起本脚本时自己还握着运行锁，共用会自锁死） =====
# 与运行锁同款三段演进：O_EXCL 原子抢锁 + PID 验活 + 空锁窗口只重试不删除
import ctypes
import atexit
_LOCK = os.path.join(BASE, '下载锁.lock')
def _pid_alive(pid):
    k = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
    if k:
        ctypes.windll.kernel32.CloseHandle(k)
        return True
    return ctypes.windll.kernel32.GetLastError() == 5   # 拒绝访问也=进程活着
def _清锁():
    try:
        if os.path.exists(_LOCK) and open(_LOCK).read().strip() == str(os.getpid()):
            os.remove(_LOCK)
    except Exception:
        pass
def _拿锁():
    """原子抢锁：O_EXCL 独占创建——同毫秒双启动也只放行一个（防两个下载器双写同一集文件）"""
    for _ in range(3):
        try:
            fd = os.open(_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return
        except FileExistsError:
            try:
                old = int(open(_LOCK).read().strip() or 0)
            except Exception:
                old = 0
            if old and old != os.getpid() and _pid_alive(old):
                print(f'⚠ 已有下载任务在跑（PID {old}），本次拒绝启动——成品会自动断点续传，稍后再跑即可')
                sys.exit(5)
            if not old:
                # 锁文件是空的：对方刚 O_EXCL 抢到锁还没写入 PID（微秒级窗口），
                # 删了会偷走人家刚到手的锁反而制造双开——只重试等它写完
                time.sleep(0.5)
                continue
            print(f'  （清理崩溃残留锁：旧 PID {old} 已不存在，自动恢复）')
            try:
                os.remove(_LOCK)
            except OSError:
                pass
            time.sleep(0.5)
    print('⚠ 下载锁状态异常，放弃本次启动（防止不明并发互踩）')
    sys.exit(5)
_拿锁()
atexit.register(_清锁)
TIMEOUT = 20
RETRIES = 3
SEG_GAP = 0.15          # 每个分片之间歇 0.15 秒，降低请求密度

def get(url):
    """带重试的 GET"""
    last = ''
    for _ in range(RETRIES):
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            if r.status_code == 200:
                return r
            last = f'HTTP {r.status_code}'
        except Exception as e:
            last = str(e)[:60]
        time.sleep(1)
    raise RuntimeError(f'请求失败({last})：{url[:100]}')

def load_episodes(txt_path):
    """把 m3u8链接.txt 解析成 [(季, 集号, 标题, 主链接, [备选...]), ...]，按文件顺序
    文件格式：=====【季】===== / 第N集 标题  [清晰度] / 主链接行 / 备选[清晰度] 行"""
    eps, cur, season = [], None, '正片'
    with open(txt_path, encoding='utf-8', errors='ignore') as f:
        for line in f:
            s = line.strip()
            h = re.match(r'^=+\s*【(.+?)】', s)
            if h:
                season = h.group(1)
                continue
            m = re.match(r'^第(\d+)集\s*(.*?)\s*\[', s)
            if m:
                if cur and cur[3]:
                    eps.append(cur)
                cur = [season, m.group(1), m.group(2) or f'第{m.group(1)}集', '', []]
                continue
            if cur is None:
                continue
            if s.startswith('http') and not cur[3]:
                cur[3] = s
            elif s.startswith('备选'):
                u = s.split(']', 1)[-1].strip() if ']' in s else s[2:].strip()
                if u.startswith('http'):
                    cur[4].append(u)
    if cur and cur[3]:
        eps.append(cur)
    return eps

def load_playlist(url):
    """下载 m3u8 播放列表；若是 master 列表（含子列表）自动跟进下一层"""
    text = get(url).text
    if '#EXT-X-STREAM-INF' in text:
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith('#'):
                print(f'  主列表 -> 跟进子列表：{line[:80]}')
                return load_playlist(urljoin(url, line))
    return text, url

def parse_key(line, base_url):
    """解析 #EXT-X-KEY 行 -> (密钥bytes, IV或None)；无加密返回 None"""
    method = re.search(r'METHOD=([A-Za-z0-9-]+)', line)
    method = method.group(1) if method else 'NONE'
    if method.upper() == 'NONE':
        return None
    if method.upper() != 'AES-128':
        raise RuntimeError(f'暂不支持的加密方式：{method}')
    uri = re.search(r'URI="([^"]+)"', line).group(1)
    key = get(urljoin(base_url, uri)).content
    iv_m = re.search(r'IV=0[xX]([0-9a-fA-F]+)', line)
    iv = bytes.fromhex(iv_m.group(1).zfill(32)) if iv_m else None
    print(f'  检测到 AES 加密，密钥 {len(key)} 字节' + ('' if iv_m else '（无 IV，按段序号生成）'))
    return (key, iv)

def parse_segments(text, base_url):
    """解析全部 ts 分片：拼成绝对地址，按文件名去重（同名分片只留第一次出现）"""
    segs, seen, media_seq = [], set(), 0
    for line in text.splitlines():
        s = line.strip()
        m = re.match(r'#EXT-X-MEDIA-SEQUENCE:(\d+)', s)
        if m:
            media_seq = int(m.group(1))
        if not s or s.startswith('#'):
            continue
        absu = urljoin(base_url, s)
        name = re.sub(r'[?#].*$', '', absu).rsplit('/', 1)[-1]
        if name in seen:                    # 去重：同一分片只下一次
            continue
        seen.add(name)
        segs.append((media_seq + len(segs), name, absu))
    return segs

def decrypt_factory(key_info):
    """返回 解密函数(数据, 段序号)->明文；无加密时原样返回"""
    if not key_info:
        return lambda data, seq: data
    try:
        from Crypto.Cipher import AES
    except ImportError:
        raise SystemExit('\n[需要解密库] 在命令行执行：pip install pycryptodome  然后重跑本脚本')
    key, fixed_iv = key_info
    def dec(data, seq):
        iv = fixed_iv if fixed_iv is not None else seq.to_bytes(16, 'big')
        data = AES.new(key, AES.MODE_CBC, iv).decrypt(data)
        pad = data[-1]                      # 去 PKCS7 填充
        if 0 < pad <= 16 and data.endswith(bytes([pad]) * pad):
            data = data[:-pad]
        return data
    return dec

def download_segments(segs, decrypt, seg_dir):
    """逐个下载分片到该集的文件夹；已存在且非空的分片直接跳过（断点续传+防重复下载）"""
    total = len(segs)
    print(f'共 {total} 个分片（已去重），保存到 {seg_dir}')
    os.makedirs(seg_dir, exist_ok=True)
    fail = []
    for n, (seq, name, url) in enumerate(segs, 1):
        path = os.path.join(seg_dir, f'seg_{n:04d}.ts')
        if not (os.path.exists(path) and os.path.getsize(path) > 0):
            data = None
            for _ in range(RETRIES):
                try:
                    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
                    if r.status_code == 200 and r.content:
                        data = decrypt(r.content, seq)
                        break
                except Exception:
                    time.sleep(1)
            if data is None:
                fail.append(n)
            else:
                with open(path, 'wb') as f:
                    f.write(data)
            time.sleep(SEG_GAP)
        pct = n * 100 // total
        bar = '█' * (pct // 4) + '·' * (25 - pct // 4)
        print(f'\r{bar} {pct:3d}%  [{n}/{total}]', end='', flush=True)
    print()
    return fail

def merge_segments(total, seg_dir, out_base):
    """按顺序把分片二进制拼接成一个完整视频文件，out_base=输出路径（不含扩展名）"""
    out_path = out_base + '.ts'
    ok = 0
    with open(out_path, 'wb') as out:
        for n in range(1, total + 1):
            p = os.path.join(seg_dir, f'seg_{n:04d}.ts')
            if os.path.exists(p) and os.path.getsize(p) > 0:
                with open(p, 'rb') as f:
                    out.write(f.read())
                ok += 1
    size = os.path.getsize(out_path) / 1024 / 1024
    print(f'\n已把 {ok}/{total} 个分片合并成完整视频 -> {out_path}（{size:.1f} MB）')
    if ok < total:
        print(f'⚠ 有 {total - ok} 个分片缺失（视频中会有轻微跳帧），重跑本脚本会自动补齐后再合并')
    return out_path

def clean_seg_dir(seg_dir):
    """该集成品(mp4)确认到手后，删掉分片缓存文件夹（每集约占成品同体积空间）"""
    if seg_dir and os.path.isdir(seg_dir):
        try:
            shutil.rmtree(seg_dir)
            print(f'  分片缓存已清理：{seg_dir}')
        except OSError as e:
            print(f'  ⚠ 分片缓存清理失败：{e}')

def to_mp4(ts_path, seg_dir=None):
    """有 ffmpeg 就无损转封装成 mp4（-c copy 不重新编码，秒转）；没有则打印安装提示"""
    if not shutil.which('ffmpeg'):
        print('\n[提示] 系统没装 ffmpeg，暂时保持 ts 格式。想转全播放器通吃的 mp4：')
        print('  自动：命令行执行  winget install Gyan.FFmpeg  装完重跑本脚本即可')
        print('  手动：ffmpeg -i "xxx.ts" -c copy "xxx.mp4"')
        return
    mp4 = os.path.splitext(ts_path)[0] + '.mp4'
    print('\n===== ⑤ 转封装为 mp4（无损秒转） =====')
    r = subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-stats', '-i', ts_path,
                        '-c', 'copy', '-movflags', '+faststart', mp4],
                       capture_output=True, text=True)
    if r.returncode == 0 and os.path.exists(mp4):
        size = os.path.getsize(mp4) / 1024 / 1024
        print(f'完成 -> {mp4}（{size:.1f} MB）')
        try:
            os.remove(ts_path)              # mp4 成品到手，删掉同体积的 ts 原片省一半空间
            print('  ts 原片已自动删除')
        except OSError as e:
            print(f'  ⚠ ts 原片删除失败：{e}')
        clean_seg_dir(seg_dir)              # 分片缓存也一起清（mp4 已验证可重新出发）
    else:
        print(f'[失败] ffmpeg 转换出错：\n{(r.stderr or "")[-500:]}')

if __name__ == '__main__':
    print('===== ① 从 m3u8链接.txt 解析全部集数 =====')
    eps = load_episodes(M3U8_TXT)
    if not eps:
        sys.exit(f'[错误] {M3U8_TXT} 里没解析到任何集数，请先运行 main.py 抓取')
    multi = len({e[0] for e in eps}) > 1          # 多季节目集号会重名，标签带上季名
    for e in eps:
        e[0] = f'{e[0]}第{e[1]}集' if multi else f'第{e[1]}集'
    print(f'解析到 {len(eps)} 集，逐集下载（成品已存在的自动跳过）')
    done, fail_list = 0, []
    for i, (label, _, title, url, mirrors) in enumerate(eps, 1):
        safe = re.sub(r'[\\/:*?"<>|]', '_', f'{label}_{title}')[:60]
        out_base = os.path.join(BASE, safe)
        if os.path.exists(out_base + '.mp4') or os.path.exists(out_base + '.ts'):
            if os.path.exists(out_base + '.mp4') and os.path.exists(out_base + '.ts'):
                try:
                    os.remove(out_base + '.ts')   # mp4 已在：顺手清掉历史遗留的同体积 ts 原片
                    print(f'\n[{i}/{len(eps)}] {label} 已有 mp4，清掉遗留 ts 原片，跳过')
                except OSError:
                    print(f'\n[{i}/{len(eps)}] {label}（{title}）已有成品，跳过')
            else:
                print(f'\n[{i}/{len(eps)}] {label}（{title}）已有成品，跳过')
            if os.path.exists(out_base + '.mp4'):
                clean_seg_dir(os.path.join(SEG_DIR, label))   # 之前完成的集：分片缓存也一并清
            done += 1
            continue
        print(f'\n===== [{i}/{len(eps)}] {label}（{title}） =====')
        print('===== ② 下载并解析 m3u8（主链接失效自动换备选镜像） =====')
        text = real = segs = None
        for u in [url] + mirrors:
            try:
                text, real = load_playlist(u)
                segs = parse_segments(text, real)
                if segs:
                    break
            except Exception as ex:
                print(f'  链接不可用：{str(ex)[:60]}')
                text = None
        if text is None or not segs:
            print(f'⚠ {label} 所有链接都失效（m3u8 链接有时效，一般几小时）。'
                  f'重跑 main.py 可换新链接（断点已有的集不会自动刷新，需删对应断点json重抓）')
            fail_list.append(label)
            continue
        print(f'  解析出 {len(segs)} 个分片')
        key_info = None
        for line in text.splitlines():
            if line.startswith('#EXT-X-KEY'):
                key_info = parse_key(line, real)
                break
        print(f'===== ③ 逐个下载 ts 分片（断点续传） =====')
        fail = download_segments(segs, decrypt_factory(key_info), os.path.join(SEG_DIR, label))
        if fail:
            print(f'失败 {len(fail)} 个：分片 {fail[:20]}{"..." if len(fail) > 20 else ""}')
        print('===== ④ 合并为完整视频 =====')
        to_mp4(merge_segments(len(segs), os.path.join(SEG_DIR, label), out_base))
        done += 1
    print(f'\n===== 全部完成：成功 {done}/{len(eps)}，链接失效 {len(fail_list)} 集 {fail_list if fail_list else ""} =====')
