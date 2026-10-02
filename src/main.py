# -*- coding: utf-8 -*-
"""
自动选集下载 · 长视频平台 —— Agent 模式版
（思路源自一个自用的全自动窗口化爬取脚本：搜索 → 自动选正片 → 抓全部季与集 m3u8 → 逐集下载 ts 分片合并成 mp4；
 本版在指令版基础上完成 Agent 化改造：参数化、防挂死、语义化退出码、原子单实例锁、机器可读报告、自动衔接下载）
说明：这是脱敏后的学习参考代码。代码里用到的站点选择器、URL、监听关键词一律以 ** 抽象化，
     「请把你的目标站点适配进来再用」。对着真实站点跑会报找不到元素，这在意料之中。
用法（手动在命令行/编辑器终端里运行，跑完自动结束，绝不后台常驻；关闭即停止，重跑自动续传）：
  agent 模式（参数必须成对给全，缺 --name 或缺 --yes 都会直接退出码 4 拒绝运行，防止挂在交互问答上）：
    python main.py --name 某剧 --index 1 --yes                # 全部季与集 + 抓完自动调起下载器
    python main.py --name 某剧 --index 1 --eps latest --yes    # 只抓全剧最新一集
    python main.py --name 某剧 --index 1 --eps 3-8 --yes       # 只抓第3~8集（按季内集号）
    python main.py --name 某剧 --index 1 --max-eps 3 --yes     # 先试抓前3集（护栏）
    多候选未给 --index 时：打印候选列表后退出码 2 结束（由 agent 拿列表问用户）
    python main.py --name 某剧 --index 1 --yes --no-download   # 抓完不自动下载
  交互模式（不带任何参数）：与自用版完全一致，搜索框输入、列表选序号、确认批量。
退出码约定（agent 程序化处置）：0=完成 1=无结果/没匹配到集数 2=多候选待选择 3=--index 越界 4=参数不全 5=已有实例在跑
铁律（运行边界）：本脚本只在你手动运行时执行本次任务——
  · 断点（m3u8结果_*.json）只用于"下次手动运行时"继续补，不会自动触发；
  · 补断点/重跑跳过已抓已下载的集，全部发生在你手动启动之后。
"""
import argparse
import json
import os
import random
import re
import subprocess
import sys
import time
import traceback
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass
from DrissionPage import ChromiumPage
ap = argparse.ArgumentParser(description='长视频平台剧集抓取（不带参数=交互模式，带参数=agent模式）')
ap.add_argument('--name', help='要搜索的节目名（不给=交互输入）')
ap.add_argument('--index', type=int, help='进入搜索列表第几个（不给且多候选=退出码2）')
ap.add_argument('--max-eps', type=int, default=0, help='只抓前N集（0=不限制，护栏参数）')
ap.add_argument('--yes', action='store_true', help='跳过批量抓取确认（agent 用）')
ap.add_argument('--no-download', action='store_true', help='抓完不自动调起下载器（默认自动下载，防m3u8链接过期）')
ap.add_argument('--eps', default='', help='集数过滤：latest=全剧最新一集；也支持 5 / 3-8 / 1,4,9-12（按季内集号，多季节会命中多季同号集）')
ARGS = ap.parse_args()
if ARGS.yes and not ARGS.name:    # 防挂死：--yes 会跳过确认，但没 --name 仍会卡在交互问答上
    print('⚠ --yes 必须配合 --name 使用（agent 调用缺 --name 会挂在 input 上）')
    sys.exit(4)                   # 退出码4=参数不全
if ARGS.name and not ARGS.yes:    # 防挂死另一面：agent 无法应答"要批量抓取吗"的确认
    print('⚠ --name 必须配合 --yes 使用（agent 调用缺 --yes 会挂在批量确认的 input 上）')
    sys.exit(4)                   # 退出码4=参数不全

# ===== 单实例锁：防双开抱死内存（曾因残留浏览器进程把机器卡死）+ 崩溃残留锁自动清理=自我修复 =====
# 三段演进：①"先检查锁存在再创建"有 TOCTOU 竞态（同毫秒双启动双双放行）→
#          ② os.open(O_CREAT|O_EXCL) 原子抢锁，独占创建由内核保证 →
#          ③ O_EXCL 成功到写入 PID 之间的"空锁窗口"：读到空 PID 只重试绝不删（删=偷走对方刚到手的锁反而制造双开）
import ctypes
import atexit
BASE = os.path.dirname(os.path.abspath(__file__))
LOCK = os.path.join(BASE, '运行锁.lock')
def _pid_alive(pid):
    k = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
    if k:
        ctypes.windll.kernel32.CloseHandle(k)
        return True
    return ctypes.windll.kernel32.GetLastError() == 5   # 拒绝访问也=进程活着
def _清锁():
    try:
        if os.path.exists(LOCK) and open(LOCK).read().strip() == str(os.getpid()):
            os.remove(LOCK)
    except Exception:
        pass
def _读锁PID():
    try:
        return int(open(LOCK).read().strip() or 0)
    except Exception:
        return 0
def _拿锁():
    """原子抢锁：O_EXCL 独占创建——两个定时任务同一毫秒启动也只放行一个，
    另一个必然拿到 FileExistsError 走占用分支（先检查后写入的老写法防不住这种竞态）"""
    for _ in range(3):                        # 最多3轮：防清理残留锁时互相竞争空转
        try:
            fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return                             # 抢到锁，独占成立
        except FileExistsError:                # 锁已被占：看占的人是死是活
            old = _读锁PID()
            if old and old != os.getpid() and _pid_alive(old):
                print(f'⚠ 已有实例在跑（PID {old}），本次拒绝启动——等它结束，或确认卡死后在任务管理器结束该进程再试')
                sys.exit(5)                    # 退出码5=已有实例在跑（并发拒绝）
            if not old:
                # 锁文件是空的：对方刚 O_EXCL 抢到锁还没来得及写入 PID（微秒级窗口）。
                # 绝不能删——删了等于偷走人家刚到手的锁，反而制造双开；只重试等它写完
                time.sleep(0.5)
                continue
            print(f'  （清理崩溃残留锁：旧 PID {old} 已不存在，自动恢复）')
            try:
                os.remove(LOCK)
            except OSError:
                pass
            time.sleep(0.5)
    print('⚠ 运行锁状态异常，放弃本次启动（防止不明并发互踩）')
    sys.exit(5)
_拿锁()
atexit.register(_清锁)   # 正常退出/异常退出都解锁；强杀进程时残留→下次启动自愈

HOME = 'https://target-site.example.com/'   # ← 请改成你的目标站点首页
VID_URL_PAT = r'/[a-z]*\d*\.html'           # ← 正片页 URL 特征（封面页没有它=会自动播花絮），按目标站点适配
if ARGS.name:
    # 防自启动告知：agent 模式第一行就亮明本次调用身份与范围（是否续传/限量）
    print('【agent 模式】节目=', ARGS.name,
          '| 序号=', ARGS.index if ARGS.index is not None else '未给（多候选将退出码2）',
          '| 限量=', f'前{ARGS.max_eps}集' if ARGS.max_eps else '不限',
          '| 集数过滤=', ARGS.eps or '不限',
          '| 自动批量=', '是' if ARGS.yes else '否',
          '；断点续传自动生效')
page = ChromiumPage()
page.get(HOME)
kw = (ARGS.name or '').strip() or input('请输入要搜索的节目名（动漫/综艺都行）：').strip()
sb = None
for _ in range(5):               # 页面没开出来/搜索框没渲染完：轮询等，防直接崩
    sb = page.ele('#**-input')
    if sb:
        break
    time.sleep(2)
if sb is None:
    raise SystemExit('搜索框没找到，页面可能没加载出来，重跑一次脚本')
sb.input(kw + '\n')
time.sleep(8)   # 等搜索结果页渲染完成
print('当前页面：', page.url)
# ===== ② 搜索结果（动漫/综艺同一套新版元素）=====
# 选择器占位（脱敏抽象）：原脚本里的私有类名一律 ** 化——
# 适配时把 ** 换成你目标站点的真实类名/属性即可（结构骨架与逻辑不用动）。
# 卡片标注：从标题元素往上爬到卡片边界（和"正片入口"同一套爬法），拿整卡文字提取类型/年份/进度
js_card = r"""
const t = this;
let p = t.parentElement;
let txt = t.textContent.trim();
while (p && p !== document.body) {
  const others = [...p.querySelectorAll('span[class*="**"], p[class*="**"]')]
    .filter(x => x !== t);
  if (others.length) break;                  // 爬到含其他标题的容器 = 爬出卡片了
  txt = p.textContent.trim().replace(/\s+/g, ' ');
  p = p.parentElement;
}
return txt;
"""
def card_txt(e):
    """标题元素所在卡片的整卡文字（含类型角标/年份/集数等副标题信息）"""
    try:
        return str(e.run_js(js_card) or '')
    except Exception:
        return ''
def biaozhu(kz):
    """从卡片文字提取标注：优先抓页面上那行"年份 类型 地区 语言"副标题（和肉眼所见一致），
    抓不到再退回零散特征拼接——整卡文字里混着简介词，零散提取会拼出假标注"""
    m = re.search(r'((?:19|20)\d{2}\s*(?:电视剧|电影|动漫|综艺|纪录片|少儿|短剧)[^，。;；！!？?]{0,14})', kz)
    if m:
        return re.sub(r'\s+', ' ', m.group(1)).strip()
    parts = []
    t = re.search(r'(纪录片|动漫|电影|电视剧|综艺|短剧|少儿)', kz)
    if t:
        parts.append(t.group(1))
    y = re.search(r'(?:19|20)\d{2}(?!\d)', kz)
    if y:
        parts.append(y.group(0))
    g = re.search(r'(更新至\s*\d+\s*集|全\s*\d+\s*集|已完结|\d+\s*集全|更新至\s*\d+\s*话)', kz)
    if g:
        parts.append(re.sub(r'\s+', '', g.group(1)))
    return ' · '.join(parts)
# 播大卡入口验证：播放按钮/选集格子必须在卡片自己范围内找——
# 全页搜"立即播放"会匹配到别的卡（某剧实测：播卡是"立即预约"预告卡，
# 全页搜到的是第三张正剧大卡的按钮，结果选热播卡却进了正剧页）
js_ka = r"""
const t = this;
let p = t.parentElement;
while (p && p !== document.body) {
  const others = [...p.querySelectorAll('span[class*="**"], p[class*="**"]')]
    .filter(x => x !== t);
  if (others.length) break;                  // 爬到含其他标题的容器 = 爬出卡片了
  const nums = [...p.querySelectorAll('a,div,span')]
    .filter(x => !x.children.length && x.getClientRects().length > 0 && /^\d{1,3}$/.test(x.textContent.trim()));
  if (nums.length >= 5) {
    window.__kaEle = nums.find(x => x.textContent.trim() === '1') || nums[0];
    return '格子';
  }
  const b = [...p.querySelectorAll('a,div,span,button')]
    .find(x => !x.children.length && x.getClientRects().length > 0 && x.textContent.trim() === '立即播放');
  if (b) { window.__kaEle = b; return '播放'; }
  p = p.parentElement;
}
return '无入口';
"""
items = []   # (标题, 可点击元素, 卡片文字)——卡片文字用来分辨同名不同型（动漫/电影/电视剧）
hot_title = page.ele('xpath://**[**(@class,"**")]')      # "正在热播"大卡的节目名
if hot_title and (hot_title.text or '').strip():
    kakou = str(hot_title.run_js(js_ka) or '')
    rk = hot_title.run_js('return window.__kaEle || null') if kakou != '无入口' else None
    if rk is None:
        print('（播卡只有"立即预约/预告"，没有正片入口，跳过不进列表）')
    else:
        items.append(('【播】' + hot_title.text.strip(), rk, card_txt(hot_title)))
# 新版搜索页两套标题并存：正主各季与底部"相关影视"各是一套类名
# （搜"某剧"时相关区会混入大量别的剧，必须按搜索词过滤）
biaoti = page.eles('xpath://**[**(@class,"**")]') or []
sums = page.eles('xpath://**[**(@class,"**")]') or []
hebing = list(sums) + list(biaoti)          # 合并候选：正主在前，相关影视在后
# 按搜索词过滤：标题里含搜索词的才是本尊/同系各季；一个都不含就退回全部，保证有得选
benzun = [e for e in hebing if kw in ((e.attr('title') or e.text or '').strip())]
if hebing and len(benzun) < len(hebing):
    print(f'（按"{kw}"过滤：{len(hebing)} 条里匹配 {len(benzun)} 条）')
hot_name = (hot_title.text or '').strip() if hot_title else ''
if not items or not items[0][0].startswith('【播】'):
    hot_name = ''                       # 热播卡没进列表（预约/预告被跳过）：不再按它去重
n_yuyue = 0
for e in (benzun or hebing):
    t = (e.attr('title') or e.text or '').strip()
    if not t or t == hot_name:          # 热播已单独加过，跳过同名
        continue
    kz = card_txt(e)
    if '立即预约' in kz and str(e.run_js(js_ka) or '') == '无入口':
        n_yuyue += 1                    # "立即预约"预告卡：没有正片可抓，不进列表
        continue
    items.append((t, e, kz))            # 存标题元素本身，点击时 js 事件冒泡到卡片
if n_yuyue:
    print(f'（{n_yuyue} 张"立即预约"预告卡没有正片入口，已跳过）')
print(f'\n可进入的节目共 {len(items)} 个：')
for i, (t, _, kz) in enumerate(items, 1):
    bz = biaozhu(kz)
    print(i, '|', t + (f'（{bz}）' if bz else '（类型未知）'))
# ===== ③ 选择进播放页 =====
if not items:
    raise SystemExit('搜索结果里没有可进入的节目（可能全是预约/预告卡，或页面还没加载出来）')
if ARGS.name:                    # agent 模式：序号走参数，绝不卡在 input 上
    if ARGS.index is not None:
        if not 1 <= ARGS.index <= len(items):
            print(f'--index={ARGS.index} 越界：只有 1~{len(items)} 个可进入的节目')
            sys.exit(3)          # 退出码3=参数越界
        xuanz = ARGS.index
    elif len(items) == 1:
        print('（只有一个候选，自动进入）')
        xuanz = 1
    else:
        print(f'（有 {len(items)} 个候选，用 --index 序号重跑即可进入）')
        sys.exit(2)              # 退出码2=多候选待选择
else:                            # 交互模式：原样保留重输循环
    while True:                  # 序号乱输/越界：不直接崩，重新提示输入
        xuanz = input('请输入要进入的节目序号：').strip()
        if xuanz.isdigit() and 1 <= int(xuanz) <= len(items):
            xuanz = int(xuanz)
            break
        print(f'序号无效，请输入 1~{len(items)} 之间的数字')
name, ele, _ = items[xuanz - 1]
if ele is None:
    print('该选项元素没找到，换个序号试试')
else:
    # 正片入口：优先找卡片上的选集格子"1"（链接直达第1集正片页）。
    # 实测点标题、点"立即播放"都会落封面页自动播花絮（两种都中过招）；
    # 选集格子似乎只有热播大卡才有，普通季的小卡片可能没有——找不到就打印诊断
    js_zp = r"""
const t = this;
let p = t.parentElement;
let lv = 0, numN = 0;
while (p && p !== document.body) {
  lv++;
  const others = [...p.querySelectorAll('span[class*="**"], p[class*="**"]')]
    .filter(x => x !== t);
  if (others.length) break;                  // 爬到含其他标题的容器 = 爬出卡片了
  const nums = [...p.querySelectorAll('a,div,span')]
    .filter(x => !x.children.length && /^\d{1,3}$/.test(x.textContent.trim()));
  if (nums.length > numN) numN = nums.length;
  const one = nums.length >= 5 ? nums.find(x => x.textContent.trim() === '1') : null;
  if (one) {
    const a = one.tagName === 'A' ? one : one.closest('a[href]');
    window.__zpEle = one;
    window.__zpHref = a ? a.href : '';
    return '选集格子1' + (a ? '（带链接）' : '') + '，爬了' + lv + '层';
  }
  const b = [...p.querySelectorAll('a,div,span,button')]
    .find(x => !x.children.length && x.textContent.trim() === '立即播放');
  if (b) {
    window.__zpEle = b;
    window.__zpHref = '';
    return '只有"立即播放"（兜底），爬了' + lv + '层、数字格子' + numN + '个';
  }
  p = p.parentElement;
}
return '卡片里没格子也没按钮，爬了' + lv + '层、最多见过' + numN + '个数字格子';
"""
    try:                          # 入口探测异常兜底：降级成点标题，后面还有花絮页补救
        how = str(ele.run_js(js_zp) or '')
        href = ele.run_js('return window.__zpHref || ""') or ''
        zp = ele.run_js('return window.__zpEle || null')
    except Exception:
        how, href, zp = '', '', None
    print('正片入口：', how or '（探测失败，降级点标题）')
    if href:
        page.get(href)                        # 格子自带正片链接：直接访问，比点击稳
        play_tab = page
    else:
        if zp:
            zp.click(by_js=True)
        else:
            ele.click(by_js=True)             # js 点击避免悬浮层遮挡
        try:
            page.wait.new_tab(timeout=10)
            play_tab = page.latest_tab
        except Exception:
            play_tab = page     # 没开新标签页就是当前页跳转
    print(f'已进入【{name}】：', play_tab.url)
time.sleep(5)       # 先等播放页渲染 5 秒左右，右侧集数面板才会出现
js_scroll = '''
(() => {
  const box = document.querySelector('#**') ||
              document.querySelector('[dr-id="List"]') ||
              document.querySelector('[class*="**"]');
  if (!box) return 'not-found';
  box.scrollTop = box.scrollHeight;
  setTimeout(() => { box.scrollTop = 0; }, 800);
  return 'ok, height=' + box.scrollHeight;
})()
'''
js_lock = """
const items = [...document.querySelectorAll('div[class*="**"][**]')]
  .filter(e => e.getClientRects().length > 0);      // 只统计真实显示出来的
if (!items.length) return 'no-item';
const scrollAncestor = e => {
  for (let p = e.parentElement; p && p !== document.body; p = p.parentElement)
    if (p.scrollHeight > p.clientHeight + 30) return p;
  return null;
};
const groups = new Map();                            // 滚动父类 -> 组内集数
for (const it of items) {
  const box = scrollAncestor(it);
  if (!box) continue;
  if (!groups.has(box)) groups.set(box, []);
  groups.get(box).push(it);
}
if (!groups.size) {                                  // 电影类短列表：条目少一屏渲染完、容器不可滚动，
  const fb = items[0].closest('.**') ||     // 找不到可滚动父类，closest 直接锁定（不判断可滚）
             items[0].closest('[id*="**"]');
  if (!fb) return 'no-box';
  window.__list = fb;
  return '锁定(短列表) | 条目=' + items.length +
         ' | 高度=' + fb.scrollHeight + '/' + fb.clientHeight;
}
let best = null, bestN = 0;                          // 集数最多的组 = 要抓的列表
for (const [bx, arr] of groups)
  if (arr.length > bestN) { best = bx; bestN = arr.length; }
window.__list = best;
const name = best.id ? '#' + best.id
                     : '.' + String(best.className).trim().split(/\\s+/).join('.');
return '锁定 ' + name + ' | 本组集数=' + bestN + ' | 候选父类=' + groups.size +
       '组 | 高度=' + best.scrollHeight + '/' + best.clientHeight;
"""
js_center = """
const b = window.__list || window.__box;
if (!b) return 'null';
const r = b.getBoundingClientRect();
return JSON.stringify([Math.round(r.x + r.width / 2), Math.round(r.y + r.height / 2)]);
"""
def wheel(tab, dy):
    """模拟真人滚轮：把鼠标放到列表中心滚动（CDP 输入事件，网页当真人处理）
    dy<0 往上滚 = 逼页面补加载更早的批次；dy>0 往下滚 = 把已加载的扫一遍"""
    c = json.loads(tab.run_js(js_center) or 'null')
    if c:
        tab.run_cdp('Input.dispatchMouseEvent', type='mouseWheel',
                    x=c[0], y=c[1], deltaX=0, deltaY=dy)
js_pick = """
const b = window.__list;
if (!b) return '[]';
const rb = b.getBoundingClientRect();
const pick = e => Math.round(e.getBoundingClientRect().top - rb.top + b.scrollTop);   // 加 scrollTop = 列表内绝对位置（不随滚动变）
let rows = [...b.querySelectorAll('div[class*="**"][**]')]
  .map(e => {
    const t = e.querySelector('div[class*="**"][**]');
    return t ? [pick(e), t.getAttribute('title')] : null;
  })
  .filter(x => x);
if (!rows.length)                                    // 兜底：没有外层卡片就直接抓标题层
  rows = [...b.querySelectorAll('div[class*="**"][**]')]
    .map(e => [pick(e), e.getAttribute('title')]);
return JSON.stringify(rows);
"""
js_top = "const b = window.__list; if (!b) return 'none'; b.scrollTop = 0; return 'ok';"
def snapshot():
    """抓当前渲染出来的所有集数：标题 + 它在列表内的绝对位置"""
    raw = json.loads(play_tab.run_js(js_pick) or '[]')
    return [(t, top) for top, t in raw if t]
# 等格子渲染（只等5秒可能面板还没出来抓了个空）：轮询最多再等 6 x 3 秒
jisu = []
for _ in range(6):
    jisu = play_tab.eles('xpath://**[**(@class,"**") and @**]')
    if jisu:
        break
    time.sleep(3)
mode = '格子'
found, ep_list, yishou = {}, [], set()
def you_zhengge():
    """页面上有没有"数字格子"（正片集数格子可见文字是纯数字；花絮卡的文字是长标题）"""
    js_y = r"""return [...document.querySelectorAll('div[class*="**"][**]')]
  .some(e => e.getClientRects().length > 0 && /^\\d{1,3}$/.test(e.textContent.trim()));"""
    return bool(play_tab.run_js(js_y))
if not you_zhengge() and not re.search(VID_URL_PAT, play_tab.url):
    # 落在封面页了（URL 没带视频id = 会自动播最新花絮）：在页面里找"正片第1集"入口跳过去。
    # 优先找带链接的（a 标签直接访问最稳），没有链接就点数字格子"1"/"第1集"
    js_zheng = r"""
const here = location.href;
const a = [...document.querySelectorAll('a[href*="**"]')]
  .filter(x => x.href !== here && /\\*\\*\\//.test(x.getAttribute('href') || ''))
  .filter(x => /^(第\\s*0?1\\s*[集话期]?|0?1)$/.test(x.textContent.trim()))[0];
if (a) return 'link|' + a.href;
const one = [...document.querySelectorAll('a,div,span,button')]
  .filter(x => !x.children.length && x.getClientRects().length > 0
             && /^(0?1|第\\s*0?1\\s*[集话期])$/.test(x.textContent.trim()))[0];
if (one) { one.click(); return 'clicked|点了页面上的数字1/第1集格子'; }
return 'none|页面上没找到正片第1集入口';
"""
    zheng = str(play_tab.run_js(js_zheng) or '')
    print('花絮页补救：', zheng)
    if zheng.startswith('link|'):
        play_tab.get(zheng[5:])               # 有链接直接访问，最稳
        time.sleep(6)
    elif zheng.startswith('clicked'):
        try:
            page.wait.new_tab(timeout=8)      # 有的入口会开新标签页
            play_tab = page.latest_tab
        except Exception:
            pass
        time.sleep(6)
    jisu = play_tab.eles('xpath://**[**(@class,"**") and @**]')
    print('补救后 URL：', play_tab.url)
# ===== ④ 分区面板与标签工具（格子/合体两用法共用）=====
# 滚动容器探测：从条目（格子或列表卡片）往上找第一个可滚动祖先（虚拟列表才有），
# 返回 [scrollTop, 总高, 视口高]；'no-scroll'=一屏全放下不用滚，'no-cell'=条目还没渲染
js_pos = r"""
const cell = [...document.querySelectorAll('div[class*="**"][**], div[class*="**"][**]')]
  .find(e => e.getClientRects().length > 0);
if (!cell) return 'no-cell';
let p = cell.parentElement;
while (p && p !== document.body) {
  if (p.scrollHeight > p.clientHeight + 50)
    return JSON.stringify([p.scrollTop, p.scrollHeight, p.clientHeight]);
  p = p.parentElement;
}
return 'no-scroll';
"""
# 面板定位：不认 id（面板 id 每部剧不同）——
# 从条目（格子或列表卡片）往上爬，爬到第一个含"数字段(1-30)"或"季标签"文字的祖先容器 = 分区面板
js_box = r"""
const cell = [...document.querySelectorAll('div[class*="**"][**], div[class*="**"][**]')]
  .find(e => e.getClientRects().length > 0);
if (!cell) return 'no-cell';
const hasSeg = p => [...p.querySelectorAll('*')].some(e => /^\\d+-\\d+$/.test(e.textContent.trim()));
const hasJi = p => [...p.querySelectorAll('*')].some(e => {
  const t = e.textContent.trim();
  return t.length <= 10 && e.children.length <= 2 && /第.{0,6}(季|部)|特别/.test(t);
});
let hit = null, kind = '';
for (let p = cell.parentElement; p && p !== document.body; p = p.parentElement) {
  if (hasSeg(p)) { hit = p; kind = '数字段标签'; break; }
  if (hasJi(p))  { hit = p; kind = '季标签'; break; }
}
if (!hit) { window.__box = cell.parentElement; return '锁定面板（无标签）'; }
for (let p = hit.parentElement, lv = 0; p && p !== document.body && lv < 3; p = p.parentElement, lv++) {
  const other = kind.indexOf('数字段') >= 0 ? hasJi(p) : hasSeg(p);
  if (other) { hit = p; kind += '+另一类标签'; break; }   // 外层还含另一类标签：篇章行在这层，面板升级
}
window.__box = hit;
return '锁定面板（有' + kind + '）';
"""
def biaqian():
    """面板标签按文字特征分行（类名每部剧不同不能认）：季文字/数字段/其他类型"""
    tt = json.loads(play_tab.run_js(js_tabs) or '{}')
    return tt.get('g1') or [], tt.get('g2') or [], tt.get('g3') or []
# 标签收集：优先认标签节点类（实测动漫/电视剧标签行统一是它，条目内部的VIP角标等碎片会被挡在外面），
# 没有该结构的页面退回全扫兜底；textContent 长串是父容器，被长度过滤天然排除
js_tabs = r"""
const box = window.__box;
if (!box) return '{}';
let nodes = [...box.querySelectorAll('[class*="**"]')]
  .filter(e => e.getClientRects().length > 0);
if (!nodes.length)
  nodes = [...box.querySelectorAll('*')].filter(e => e.getClientRects().length > 0);
const ts = nodes.map(e => e.textContent.trim())
  .filter(t => t && t.length <= 10 && !/^\\d{1,3}$/.test(t)
             && !/^\\d+-\\d+-/.test(t)   // 排除连体段标签（"1-3031-36"是两个段挤一起，永远是点不到的脏标签）
             && !/^更多/.test(t) && t !== '相关推荐' && t !== '操控列表' && t !== '选集');
return JSON.stringify({
  g1: [...new Set(ts.filter(t => /第.{0,6}(季|部)|特别/.test(t)))],
  g2: [...new Set(ts.filter(t => /^\\d+-\\d+$/.test(t)))],
  g3: [...new Set(ts.filter(t => !/第.{0,6}(季|部)|特别/.test(t) && !/^\\d+-\\d+$/.test(t)))]
});
"""
def dian(tname):
    """点标签切分区：在面板里找文字完全一致的显示块，点最里层；点完用选中态验证是否生效"""
    js_d = r"""
const box = window.__box;
if (!box) return 'no-box';
const want = __WANT__;
const els = [...box.querySelectorAll('*')]
  .filter(e => e.getClientRects().length > 0 && e.textContent.trim() === want);
if (!els.length) return 'not-found';
els[els.length - 1].dispatchEvent(       // 与切集点击一致：裸 click 在 React 页面经常被拦截不生效，用事件派发
  new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
return 'ok';
"""
    js_v = r"""
const box = window.__box;
if (!box) return 'no-box';
const want = __WANT__;
const sel = [...box.querySelectorAll('[class*="selected"],[class*="active"],[class*="current"]')]
  .filter(e => e.getClientRects().length > 0)
  .map(e => e.textContent.trim());
return sel.includes(want) ? '已选中' : '未选中';   // 选中态元素(如 tab-节点-selected)的文字=目标标签
"""
    jv = js_v.replace('__WANT__', json.dumps(tname, ensure_ascii=False))
    for _ in range(2):               # 最多点两次：第一次选中态没切过去就补一刀
        js = js_d.replace('__WANT__', json.dumps(tname, ensure_ascii=False))
        r = str(play_tab.run_js(js) or '')
        if r != 'ok':                # 面板可能被重渲染整树换过，重新爬锁再试一次
            play_tab.run_js(js_box)
            r = str(play_tab.run_js(js) or '')
        if r != 'ok':
            print(f'  ⚠ 标签"{tname}"没点到')
            return False
        time.sleep(2)                # 等内容整批刷新 + 选中态更新
        if str(play_tab.run_js(jv) or '') == '已选中':
            return True
        print(f'  （标签"{tname}"点击未生效，再点一次）')
    return True                      # 点都点了，条目在不在交给后面抓取环节检验
def duan_now():
    """当前季下的数字段标签（1-30/31-36）——每季分段可能不同，用前现取"""
    return biaqian()[1]
def shengcheng_ep_list():
    """found(标题->5元组) 按季分组生成 ep_list：季序=页面标签顺序，每季独立从1编号
    ep_list 元素 = (断点键, 季标签, 季内序号, 标题, 切集键)——断点键"季#集号"防不同季的"第1集"撞车"""
    for tt, key5 in sorted(found.items(), key=lambda x: (x[1][0], x[1][1], x[1][2])):
        grouped.setdefault(key5[3] or '正片', []).append(tt)
    for jn, tts in grouped.items():
        for xi, tt in enumerate(tts, 1):
            ep_list.append((f'{jn}#{xi}', jn, xi, tt, tt))
if jisu:
    # —— 格子模板分支（动漫/电视剧同一套格子元素：可见文字是数字、title 是集标题或剧情描述）——
    #    多集多季的剧格子按分区标签储藏（"1-30"/"31-36"/季标签），综艺/电影走下面剧集分支。
    #    流程 = 第一次获取（当前页全部，滚动触发懒加载）→ 逐个点标签 → 每停一处再抓一遍
    #           → 同名去重（后到覆盖先到，正确分区的排序位置盖掉兜底位置）→ 合并按序排
    print('集数列表滚动：', play_tab.run_js(js_scroll))
    time.sleep(2)
    found, grouped = {}, {}          # found: 标题->(季序,段序,格子位置,季标签,段标签)  grouped: 季标签->[标题]
    def sazi(jx, dx, jn, dn):
        """统一抓取：抓当前渲染的全部条目；面板可滚动（合集虚拟列表）就边滚边抓到到底
        普通格子页一屏全放下，自动跳过滚动。分区内按首见顺序编号（虚拟列表的抓取顺序=展示顺序）"""
        seen, xu, dao, last, no_cell = set(), 0, 0, '', 0
        for lun in range(500):                 # 上限防死循环（一屏约10条，够2800+话的段用）
            for i, d in enumerate(play_tab.eles('xpath://**[**(@class,"**") and @**]')):
                tt = (d.attr('title') or '').strip()
                if tt and not re.search(r'预告|花絮|抢先', tt) and tt not in seen:
                    seen.add(tt)
                    found[tt] = (jx, dx, xu, jn, dn)   # 同名去重：正确分区的首见顺序盖掉兜底位置
                    xu += 1
            pos = play_tab.run_js(js_pos)
            if pos == 'no-cell':               # 格子还没渲染好：等一拍再抓
                no_cell += 1                   # 防死循环：渲染不出条目也要计数，不能一直 continue
                if no_cell >= 10:              # 连续约10秒渲染不出 = 页面没加载/被风控，结束本轮去兜底
                    break
                time.sleep(1)
                continue
            no_cell = 0                        # 渲染正常就清零
            if pos == 'no-scroll':             # 一屏全放下（普通格子页）：不用滚
                break
            wheel(play_tab, random.randint(400, 800))          # 虚拟列表：往下滚一屏继续抓（滚动幅度/歇息都随机，模仿人手速，避免每次都是固定节奏被识别）
            time.sleep(random.uniform(0.45, 0.8))
            dao = dao + 1 if str(pos) == last else 0   # 滚了位置没变 = 到底了
            last = str(pos)
            if dao == 1:                       # 也可能面板被换过导致滚轮失焦，重锁再试一轮
                play_tab.run_js(js_box)
            if dao >= 2:
                break
        return len(seen)
    play_tab.run_js(js_box)          # 先锁面板：sazi 滚动收集的滚轮中心（js_center）要用 __box
    sazi(9, 9, None, None)           # 第一次获取：当前页全部；兜底组(9,9)排最后但不丢
    print(f'第一次获取（当前页）：{len(found)} 条')
    biaoqian_all = []                    # 兜底切分区用的全量标签（切集找不到格子时挨个试）
    ding = str(play_tab.run_js(js_box) or '')
    print('分区面板：', ding)
    if ding.startswith('锁定面板（有'):  # 真有标签才走分区循环；动漫单季页没标签防误点
        g1, g2, g3 = biaqian()
        jis = g1                         # 季行（"特别版"也算季）
        biaoqian_all = list(dict.fromkeys(g1 + g2 + g3))
        print('分区标签：季=', jis, '| 段=', g2 or '（无）', '| 类型=', g3 or '（无）')
        if jis:                          # 多季：外层逐季点过去，内层把这一季的数字段点全
            for jx, j in enumerate(jis):
                dian(j)
                for dx, d in enumerate(duan_now() or [None]):
                    if d:
                        dian(d)
                    sazi(jx, dx, j, d)
        elif g2:                         # 单季多段（1-30/31-36）：逐段点过去
            for dx, d in enumerate(g2):
                dian(d)
                sazi(0, dx, None, d)
        elif g3:                         # 类型标签（正片/花絮那种）：也逐个点
            for dx, d in enumerate(g3):
                dian(d)
                sazi(0, dx, None, d)
    else:
        print('（无分区面板，当前页就是全部）')
    print(f'去重合并后共 {len(found)} 条')
    shengcheng_ep_list()
else:
    # —— 先探测合体：无格子，但列表条目（综艺式）+ 分区标签（格子式）同时存在 = 合集页 ——
    #    如某合集动漫：条目是列表卡片且虚拟滚动，但按"第1季/1-30/31-60"分区储藏
    liebiao = []
    for _ in range(4):                  # 列表条目还没渲染就等，最多 4 x 3 秒
        liebiao = play_tab.eles('xpath://**[**(@class,"**") and @**]')
        if liebiao:
            break
        time.sleep(3)
    ding = str(play_tab.run_js(js_box) or '')
    if liebiao and ding.startswith('锁定面板（有'):
        mode = '合体'
        print('分区面板：', ding, '—— 列表条目+分区标签 = 合体用法')
        found, grouped = {}, {}
        g1, g2, g3 = biaqian()
        biaoqian_all = list(dict.fromkeys(g1 + g2 + g3))
        print('分区标签：季=', g1, '| 段=', g2 or '（无）', '| 类型=', g3 or '（无）')
        def shouji(jx, dx, jn, dn):
            """合体分支的分区收集：锁列表滚动容器后 JS 直滚往下扫（scrollTop 不经过鼠标，
            无悬浮遮挡问题、无头模式也能用），连续3轮无新增且滚不动=到底"""
            dingwei = play_tab.run_js(js_lock)
            for _ in range(3):
                if not str(dingwei).startswith('no'):
                    break
                time.sleep(3)
                dingwei = play_tab.run_js(js_lock)
            # 切换分区后列表可能只渲染一屏、锁到的容器暂时没滚动空间：往祖先找真滚动容器
            play_tab.run_js(r"""
let b = window.__list;
if (b && b.scrollHeight <= b.clientHeight + 30)
  for (let p = b.parentElement; p && p !== document.body; p = p.parentElement)
    if (p.scrollHeight > p.clientHeight + 30) { window.__list = p; break; }
return 'ok';
""")
            seen, xu, kong, before, last = set(), 0, 0, -1, ''
            for lun in range(300):
                for t, top in snapshot():
                    if t and t not in seen and not re.search(r'预告|花絮|抢先', t):
                        seen.add(t)
                        found[t] = (jx, dx, xu, jn, dn)
                        xu += 1
                pos = str(play_tab.run_js(r"""
const b = window.__list;
if (!b) return 'none';
b.scrollTop = b.scrollTop + __STEP__;   // JS 直滚一屏（幅度每轮随机，防固定节奏）
return JSON.stringify([b.scrollTop, b.scrollHeight]);
""".replace('__STEP__', str(random.randint(400, 700))) or ''))
                time.sleep(random.uniform(0.6, 1.1))
                kong = kong + 1 if (len(seen) == before and pos == last) else 0
                before, last = len(seen), pos
                if kong >= 3:
                    break
            print(f'  分区[{jn or ""}{"·" if jn and dn else ""}{dn or ""}]收集 {len(seen)} 条')
        if g1:                          # 逐季：点季 → 该季数字段逐段 → 每段滚动收集
            for jx, j in enumerate(g1):
                dian(j)
                for dx, d in enumerate(duan_now() or [None]):
                    if d:
                        dian(d)
                    shouji(jx, dx, j, d)
        elif g2:
            for dx, d in enumerate(g2):
                dian(d)
                shouji(0, dx, None, d)
        else:
            shouji(0, 0, None, None)    # 标签没收集到：兜底抓当前屏分区，不至于空手而归
        print(f'去重合并后共 {len(found)} 条')
        shengcheng_ep_list()
    else:
        # —— 剧集通用分支：综艺/电影都是这套元素（虚拟列表），只是列表长短不同 ——
        #    长列表（综艺47集/电视剧38集）：虚拟列表懒加载，滚轮往上顶分批补加载，两阶段收集
        #    短列表（电影3条版本）：一屏渲染完、容器不可滚动，锁定后直接抓
        mode = '剧集'
        dingwei = play_tab.run_js(js_lock)
        for _ in range(3):                  # 还没渲染出来就继续等，最多再等 3 轮 x 3 秒
            if not str(dingwei).startswith('no'):
                break
            time.sleep(3)
            dingwei = play_tab.run_js(js_lock)
        print('列表定位：', dingwei)
        found = {}    # 标题 -> 列表内绝对位置（同一坐标系，排序才准）
        if '短列表' in str(dingwei):        # 电影类：不用滚轮，这一屏就是全部
            for t, top in snapshot():
                found[t] = top
        else:                               # 长列表：滚轮两阶段收集
            def collect0():
                for t, _ in snapshot():
                    yishou.add(t)
            collect0()                      # 阶段一：往上顶，逼页面一批批补加载更早的集数
            kong = 0
            for _ in range(25):
                wheel(play_tab, -600)
                time.sleep(0.9)             # 等它请求并把这一批渲染出来
                before = len(yishou)
                collect0()
                kong = kong + 1 if len(yishou) == before else 0
                if kong >= 3:               # 连续3轮没新内容 = 更早的已经补完/不再给了
                    break
            print(f'往上补加载结束，共见到 {len(yishou)} 条，回到顶部正式扫描记录顺序...')
            play_tab.run_js(js_top)         # 先回到列表最顶
            before, kong = 0, 0
            for _ in range(30):             # 阶段二：从顶到底扫一遍，记录每条的绝对位置
                for t, top in snapshot():
                    found[t] = top          # 内容不再增减，反复覆盖值也一样
                wheel(play_tab, 300)        # 往下滚一屏
                time.sleep(0.6)
                for t, top in snapshot():
                    found[t] = top
                kong = kong + 1 if len(found) == before else 0
                before = len(found)
                if kong >= 3:               # 连续3轮没新内容 = 到底了
                    break
        for t, xu in sorted(found.items(), key=lambda x: x[1]):   # 第1期/第1条在最上
            xi = len(ep_list) + 1
            ep_list.append((f'正片#{xi}', '正片', xi, t, t))
if ARGS.max_eps and len(ep_list) > ARGS.max_eps:   # 护栏：只取前N集（单集验证+批量都受它约束）
    print(f'\n（--max-eps {ARGS.max_eps}：只取前 {ARGS.max_eps} 集，其余跳过）')
    ep_list = ep_list[:ARGS.max_eps]
print(f'\n共 {len(ep_list)} 集（{mode}流程），按季分组如下：')
now_j = None
for bk, jn, xi, t, key in ep_list:
    if jn != now_j:                     # 换季了：打一行季标题
        now_j = jn
        cnt = sum(1 for b2, j2, _, _, _ in ep_list if j2 == jn)
        print(f'\n【{jn}】共 {cnt} 集：')
    print(' ', xi, '|', t)
if not ep_list:
    raise SystemExit('未找到集数列表，请检查页面是否正常加载')
if not (mode == '格子' and len(ep_list) >= 10) and \
        not [t for _, _, _, t, _ in ep_list if re.search(r'第\d+(集|话|期)', t)]:
    # 同一部剧"正片入口"有集数面板、"花絮入口"只有花絮列表（某剧两季实测是两种布局）。
    # 格子流程抓到10条以上就当正片——格子标题是剧情描述不带"第N集"字样，
    # 而花絮/预告列表一般不到10条；列表里一条正剧格式都没有 = 八成点进了花絮/预告页
    print('⚠ 列表里没有"第N集/话/期"格式的正剧条目——这个入口八成是花絮/预告页，')
    print('  换搜索结果里的其他条目再跑；或搜完整剧名（如"某剧第二季"），让热播大卡就是本剧')
# ===== ⑤ 抓 URL 公共函数 =====
def find_key(obj, key):
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = find_key(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_key(v, key)
            if r is not None:
                return r
    return None
def extract_vinfo(packet):
    """从网络包里递归找出播放信息字典（清晰度/播放地址的官方数据结构）"""
    try:
        body = packet.response.body
        if isinstance(body, (bytes, bytearray)):
            body = body.decode('utf-8', errors='ignore')
        data = body if isinstance(body, dict) else json.loads(body)
    except Exception:
        return None
    raw = find_key(data, '**info')       # ← 信息字典的键名按目标站点适配
    if raw is None:
        return None
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return None
def capture_vinfo(tab, seconds=20):
    """在 seconds 秒内甄别网络包，抓到信息字典就返回；返回 (info, 甄别包数)"""
    deadline = time.time() + seconds
    count = 0
    while time.time() < deadline:
        try:
            packet = tab.listen.wait(timeout=2)
        except Exception:
            packet = None
        if not packet:
            continue
        count += 1
        vinfo = extract_vinfo(packet)
        if vinfo:
            return vinfo, count
    return None, count
def start_listen(tab):
    try:
        tab.listen.clear()
    except Exception:
        pass
    try:
        tab.listen.start('**')           # ← 监听关键词（按目标站点适配）
    except Exception:
        pass
def _clarity_score(text):
    """把清晰度文字转成可比较的分数，越大越清晰"""
    t = str(text or '')
    if '蓝光' in t or '4k' in t.lower():
        return 4000
    for kw, s in (('1080', 1080), ('720', 720), ('480', 480), ('360', 360)):
        if kw in t:
            return s
    return 0
def collect_m3u8(obj, ctx, out):
    """递归遍历信息字典，收集所有 m3u8 地址及其所在层的清晰度线索（字段名不固定也能关联上）"""
    if isinstance(obj, dict):
        hints = dict(ctx)
        for k in ('cname', 'clarity', 'definition', 'resolution', 'name'):
            v = obj.get(k)
            if isinstance(v, (str, int)) and str(v):
                hints.setdefault(k, str(v))
        if obj.get('vh'):                      # vh=视频高度，如 1080
            hints['vh'] = obj['vh']
        m = re.search(r'\.f(\d{4,})\.', str(obj.get('fn') or ''))   # 文件名内含档位id
        if m:
            hints['fid'] = m.group(1)
        for v in obj.values():
            collect_m3u8(v, hints, out)
    elif isinstance(obj, list):
        for v in obj:
            collect_m3u8(v, ctx, out)
    elif isinstance(obj, str) and obj.startswith('http') and '.m3u8' in obj:
        cname = ctx.get('cname') or ctx.get('clarity') or ctx.get('definition') \
            or ctx.get('resolution') or ctx.get('name') or ''
        m = re.search(r'\.f(\d{4,})\.', obj)     # URL 本身也带档位id
        out.append({'name': cname, 'vh': ctx.get('vh') or 0,
                    'fid': ctx.get('fid') or (m.group(1) if m else ''), 'url': obj.strip('`')})
def pick_m3u8(vinfo):
    """挑出清晰度最高的 m3u8，返回 (清晰度名, 地址, 全部清晰度列表)"""
    # 官方清晰度表：档位id -> (名称, 高度)
    fi_map = {}
    for fi in (vinfo.get('fl') or {}).get('fi') or []:
        fi_map[str(fi.get('id'))] = (str(fi.get('cname') or fi.get('sname')
                                      or fi.get('resolution') or ''), fi.get('height') or 0)
    found_m = []
    collect_m3u8(vinfo, {}, found_m)
    seen, uniq = set(), []
    for it in found_m:                 # 同一地址可能出现多次，去重
        if it['url'] not in seen:
            seen.add(it['url'])
            uniq.append(it)
    if not uniq:                       # 最后兜底：直接在信息文本里搜
        m = re.search(r'https?://[^"\'`\s]+?\.m3u8[^"\'`\s]*',
                      json.dumps(vinfo, ensure_ascii=False))
        if m:
            return '未知', m.group(0), []
    # 用档位id反查官方清晰度名；同一档位的多条 CDN 镜像只留第一个
    by_fid, order = {}, []
    for it in uniq:
        fid = str(it.get('fid') or '')
        cname, height = fi_map.get(fid, ('', 0))
        if cname:                      # 官方名如 '高清SDR;(1080P)'，去掉分号变成 '高清SDR(1080P)'
            it['name'] = cname.replace(';', '', 1) if ';' in cname else cname
        if not it['name'] and height:
            it['name'] = f"{height}P"
        if height and not it.get('vh'):
            it['vh'] = height
        key = fid or ('x_' + it['url'])    # 没有档位id的（如字幕）按地址算
        if key in by_fid:
            continue
        by_fid[key] = it
        order.append(it)
    order.sort(key=lambda x: ((x['vh'] if isinstance(x['vh'], (int, float)) else 0),
                              _clarity_score(x['name'])), reverse=True)
    best = order[0]
    return best['name'] or (f"{best['vh']}P" if best['vh'] else '未知'), best['url'], order
# ===== ⑥ 切集点击：统一入口，内部分流 =====
js_var_click = """
const want = __WANT__;
const b = window.__list || window.__box;
if (!b) return 'no-box';
for (const it of b.querySelectorAll('div[class*="**"][**]')) {   // 综艺/剧集列表
  if (it.getAttribute('title') === want) {
    const card = it.closest('div[class*="**"]') || it;
    card.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
    return 'clicked';
  }
}
const cands = [...b.querySelectorAll('div[class*="**"]')];         // 电视剧网格格子
let hit = cands.find(e => e.getAttribute('title') === want)
       || cands.find(e => e.textContent.trim() === want);
if (!hit && /^\\d{1,3}$/.test(want))     // 文字格式对不上时按数值比（"1"vs"01"）
  hit = cands.find(e => /^\\d{1,3}$/.test(e.textContent.trim())
                     && parseInt(e.textContent.trim(), 10) === parseInt(want, 10));
if (hit) {
  hit.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
  return 'clicked';
}
return 'not-found';
"""
def click_ep(tab, key):
    """统一切集入口：格子/剧集模板的 key 都是条目标题
    格子模板：面板整批换格子，该集在哪个分区就先照原路点回去（季标签→数字段标签）再点格子；
    剧集模板：虚拟列表会卸载滚出视口的卡片，先用记录的位置滚到目标居中等 1 秒渲染再点"""
    if mode in ('格子', '合体'):
        info = found.get(key)
        if info and (info[3] or info[4]):    # 该集属于某个分区：先把面板点回去
            if info[3]:
                dian(info[3])
            if info[4]:
                dian(info[4])
        tab.run_js(js_box)                   # 面板现锁一遍：上一集点击可能刚引发翻页导航，页面上旧的__box已死
        for _box_retry in range(3):          # 切集后面板可能重渲染空窗：锁不到就等1秒重锁，避免拿旧DOM白找
            bx = str(play_tab.run_js(js_box) or '')
            if bx.startswith('锁定'):
                break
            time.sleep(1)
        tab.run_js("window.__list = window.__box; return 'ok';")
        # 回切（尤其预热后回到第1集）时列表滚动条可能停在中间，而查找循环只往下滚，
        # 目标在最顶上会被永远漏掉：先把列表滚回顶部再开始找。
        tab.run_js("const b = window.__list; if (b) b.scrollTop = 0; return 'ok';")
        # 点击容器先强制=面板：防止 js_pos 抢先把 __list 锁到别的模块（片花等）抢走点击的优先级；
        # 合集页真有滚动容器时下面循环里的 js_pos 每轮会重新把 __list 锁回正确位置，不受影响
        js = js_var_click.replace('__WANT__', json.dumps(key, ensure_ascii=False))
        last, dao = '', 0
        for _ in range(500):                 # 普通格子第1轮就点到；合集虚拟列表边滚边找
            if str(tab.run_js(js) or '') == 'clicked':
                return True
            pos = str(play_tab.run_js(js_pos) or '')
            if pos == 'no-scroll':           # 没有滚动容器（普通格子页）：重锁面板再点一次，还不行走兜底
                tab.run_js(js_box)
                tab.run_js("window.__list = window.__box; return 'ok';")
                time.sleep(1)                # 等这一屏格子渲染出来
                if str(tab.run_js(js) or '') == 'clicked':
                    return True
                break
            if pos == 'no-cell':             # 分区格子还没渲染好：等一拍
                dao += 1
                if dao >= 10:
                    break
                time.sleep(0.5)
                continue
            tab.run_js("const b = window.__list || window.__box;"
                       "if (b) b.scrollTop = b.scrollTop + 500; return 'ok';")  # JS 直滚，不碰鼠标
            time.sleep(0.4)
            dao = dao + 1 if pos == last else 0
            last = pos
            if dao >= 3:                     # 连滚3轮位置不变 = 到底了还没找到
                break
        for tname in biaoqian_all:           # 兜底：逐个分区找过去（打印轨迹，面板翻到哪一目了然）
            print(f'  （兜底试分区"{tname}"）')
            if dian(tname):
                time.sleep(1.2)              # 等分区面板整批切换渲染（上一条目矩阵刚消失）再重锁
                tab.run_js(js_box)
                tab.run_js("window.__list = window.__box; return 'ok';")
                if str(tab.run_js(js) or '') == 'clicked':
                    return True
        print(f"  ⚠ 集数定位失败：{key}")
        return False
    # 剧集模板：先滚到目标居中
    st = int(max(0, found.get(key, 0)))
    tab.run_js("const b = window.__list; if (!b) return 'no-box';"
               f"b.scrollTop = Math.max(0, {st} - Math.round(b.clientHeight / 2));"
               "return 'ok';")
    time.sleep(1)                  # 等虚拟列表把这一屏渲染出来
    js = js_var_click.replace('__WANT__', json.dumps(key, ensure_ascii=False))
    if str(tab.run_js(js)) == 'clicked':
        return True
    print(f"  ⚠ 集数定位失败：{key}")
    return False
# ===== ⑦ 断点存档（每剧独立文件，串行抓多个节目互不覆盖；并发已被运行锁挡住） =====
results = {}                      # 断点键 -> dict(标题/清晰度/m3u8/备选)
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'm3u8链接.txt')
def state_path():
    """断点文件按剧名走：m3u8结果_剧名.json——A没抓完再去抓B，A的断点不会被B洗掉"""
    an = re.sub(r'[\\/:*?"<>|]', '', name).strip()[:40] or '默认'
    return os.path.join(BASE, f'm3u8结果_{an}.json')
def save_state():
    """断点存档：每抓到一集就写 json，重跑脚本自动跳过已抓的集，避免重复请求"""
    try:
        with open(state_path(), 'w', encoding='utf-8') as f:
            json.dump({'show': name, 'eps': results}, f, ensure_ascii=False, indent=1)
    except Exception as e:
        print(f"  ⚠ 断点存档失败：{e}")
def load_results():
    """启动时载入上次断点，已抓过的集不再重复请求；节目对不上就不用它的断点"""
    path = state_path()
    if not os.path.exists(path):  # 每剧文件还没有 → 看旧单文件是不是本节目的（老断点自动迁移）
        old = os.path.join(BASE, 'm3u8结果.json')
        if os.path.exists(old):
            try:
                with open(old, 'r', encoding='utf-8') as f:
                    d = json.load(f)
                if isinstance(d, dict) and d.get('show') == name:
                    os.replace(old, path)
                    print('  （旧单文件断点迁移为每剧独立断点）')
            except Exception:
                pass
    if not os.path.exists(path):
        return
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict) and 'eps' in data:     # 新结构（带节目名）
            shang = data.get('show')
            if shang != name:                            # 每剧独立后理论不再触发，留着兜底
                print(f"  断点是《{shang}》的，和当前节目不同，忽略从头抓")
                return
            data = data['eps']
        else:
            # 旧格式没有节目名，无法确认是哪个节目的，忽略（链接早已存在链接文件，无损失）
            print('  旧版断点格式，无法确认节目，忽略从头抓')
            return
        results.update(data)          # 键是"季#集号"字符串，直接沿用
        sheng = sum(1 for e in ep_list if e[0] not in results)   # 还没抓的集数
        print(f"  【断点续传】上次《{shang}》抓到 {len(results)} 集时断开，"
              f"本次自动续抓剩余 {sheng} 集，已抓的不重复请求")
    except Exception as e:
        print(f"  ⚠ 断点文件读取失败（忽略，从头抓）：{e}")
def save_txt():
    """每抓到一集就整体重写文件（按季分组），中途崩溃也不丢已抓结果"""
    tmp = OUT + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            now_j = None
            for bk, jn, xi, t, key in ep_list:
                r = results.get(bk)
                if not r:
                    continue
                if jn != now_j:       # 换季了：文件里也分季存放
                    now_j = jn
                    f.write(f"=====【{jn}】=====\n")
                f.write(f"第{xi}集 {r['title']}  [{r['name']}]\n{r['m3u8']}\n")
                for q in r['all'][1:]:
                    nm = q['name'] or (f"{q['vh']}P" if q['vh'] else '未知')
                    f.write(f"    备选[{nm}] {q['url']}\n")
                f.write('\n')
        os.replace(tmp, OUT)       # 先写临时文件再原子替换：文件被占用时报错但不会写坏旧内容
    except PermissionError:
        print("  ⚠ 链接文件被占用（可能正被别的程序打开），本集结果已存断点，下集会再尝试写入")
def save_one(bk, jn, xi, title, vinfo):
    nm, m3u8, all_q = pick_m3u8(vinfo)
    results[bk] = {'title': title, 'name': nm, 'm3u8': m3u8, 'all': all_q}
    extra = f"，共 {len(all_q)} 种清晰度" if len(all_q) > 1 else ''
    print(f"✅ 【{jn}】第{xi}集（{title}）[{nm}]{extra} {m3u8[:120]}")
    save_txt()
    save_state()
# ===== ⑧ 抓单集全链路（单集验证和批量抓取共用） =====
def fetch_one(bk, jn, xi, title, key, seconds=15):
    """切到指定集 -> 监听网络包 -> 提取信息字典 -> 挑最高清晰度存档。抓到返回 True"""
    global play_tab
    start_listen(play_tab)            # 清掉上一集的包，只留本次切集后的
    if not click_ep(play_tab, key):
        print(f"⚠ 点击【{jn}】第{xi}集失败，跳过")
        return False
    print(f"  已点击【{jn}】第{xi}集（{title}），等待播放信息 ...")
    time.sleep(1)
    if len(page.tab_ids) > 1:         # 若切集误开了新标签页，把监听挪过去
        lt = page.latest_tab
        if lt.tab_id != play_tab.tab_id:
            play_tab = lt
            start_listen(play_tab)
            time.sleep(2)
    vinfo, cnt = capture_vinfo(play_tab, seconds)
    if vinfo:
        print(f"  （甄别 {cnt} 个包）")
        save_one(bk, jn, xi, title, vinfo)
        return True
    print(f"⚠ 【{jn}】第{xi}集（{title}）{seconds}秒内未抓到播放信息（甄别 {cnt} 个包）")
    return False
# ===== 参数级集数过滤（--eps：latest=全剧最新一集；5 / 3-8 / 1,4,9-12=按季内集号） =====
if ARGS.eps and ep_list:
    _spec = ARGS.eps.strip()
    if _spec.lower() in ('latest', 'last', '最新'):
        _mxd = ep_list[-1]         # ep_list 已按(季,段,位置)升序排好，最后一个=最新更新集
        print(f'（--eps latest：只取全剧最新一集 →【{_mxd[1]}】第{_mxd[2]}集 {_mxd[3]}）')
        ep_list = [_mxd]
    else:
        def _eps_hit(xi):
            for _part in _spec.replace('，', ',').split(','):
                _m = re.fullmatch(r'\s*(\d+)(?:\s*-\s*(\d+))?\s*', _part)
                if _m and int(_m.group(1)) <= xi <= int(_m.group(2) or _m.group(1)):
                    return True
            return False
        _qian = len(ep_list)
        ep_list = [e for e in ep_list if _eps_hit(e[2])]
        print(f'（--eps {_spec}：{_qian} → {len(ep_list)} 集；按季内集号，多季节会命中多季同号集）')
        if not ep_list:
            raise SystemExit(f'--eps {_spec} 没匹配到任何一集，检查集号写法（如 5 / 3-8 / 1,4）')
load_results()                    # 先载入断点，已抓过的集全部跳过
# ===== ⑨ 单集验证：抓第1集的 m3u8（断点里已有就自动跳过） =====
if ep_list and ep_list[0][0] not in results:
    if len(ep_list) > 1 and click_ep(play_tab, ep_list[1][4]):
        # 预热切第2集若开了新标签页：切集逻辑是事件派发，个别分区会开新页面。
        # 开了新页就回不到旧页的 history 链上，回退方案失效，直接走点位抓取
        if len(page.tab_ids) > 1 and page.latest_tab.tab_id != play_tab.tab_id:
            print(f"  预热切第2个开了新标签页，回退方案不可用，改用点位抓取第1集...")
            fetch_one(*ep_list[0], seconds=20)
        else:
            print(f"  先切到第2个（{ep_list[1][3]}）预热，等3秒...")
            time.sleep(3)                 # 打开播放页时正在播的就是第1集，先切走再切回来才有新请求
            # 回第1集保底方案：预热切第2集 = 整页导航，history 里"第1集→第2集"只有一条记录，
            # 直接浏览器后退就回到第1集，整页重载会重新发播放信息请求（和手动点回第1集完全一样），
            # 绕开分区面板 React 点位不生效的老问题（两层段标签就栽在这）
            start_listen(play_tab)
            try:
                play_tab.back()
            except Exception:
                play_tab.run_js('history.back()')
            time.sleep(1)
            vinfo, _cnt = capture_vinfo(play_tab, seconds=10)
            if vinfo:
                bk0, jn0, xi0, t0, _ = ep_list[0]
                print(f"  （回退回到第{xi0}集，已抓回播放信息）")
                save_one(bk0, jn0, xi0, t0, vinfo)
            else:
                print("  回退未抓到播放信息，退回点位抓取...")
                fetch_one(*ep_list[0], seconds=20)
    else:
        fetch_one(*ep_list[0], seconds=20)
elif ep_list:
    print("  断点里已有第1集，跳过单集验证")
# ===== ⑩ 批量抓取剩余集数（每集之间随机歇3~6秒，防请求过密；断点自动跳过已抓的） =====
MAX_LIANBAI = 5                   # 熔断阈值：连续N集抓不到就停（防风控/防整轮白跑）
lianbai = 0
if ARGS.yes or input('\\n要批量抓取剩余集数吗？(直接回车跳过，输 y 继续)：').strip().lower() == 'y':
    todo = sum(1 for b, _, _, _, _ in ep_list[1:] if b not in results)
    print(f'===== 批量抓取开始：待抓 {todo} 集 =====')
    for bk, jn, xi, t, key in ep_list[1:]:
        if bk in results:
            continue
        try:
            ok = fetch_one(bk, jn, xi, t, key)
        except Exception:
            ok = False
            print(f"⚠ 【{jn}】第{xi}集处理出错，跳过本集继续：")
            traceback.print_exc()
        lianbai = 0 if ok else lianbai + 1
        if lianbai >= MAX_LIANBAI:  # 熔断：连续5集失败=页面/风控出问题了，硬跑只会更糟
            print(f'⛔ 连续 {MAX_LIANBAI} 集抓取失败，熔断停止批量。断点已存档，稍后重跑自动续抓')
            break
        time.sleep(random.uniform(3, 6))   # 模拟人工切集节奏（熔断退出时不歇）
print("\\n===== 保存结果 =====")
if results:
    save_txt()
ok = sum(1 for v in results.values() if v['m3u8'])
print(f"共抓到 {len(results)} 集（含 m3u8 的 {ok} 集），已保存到 {OUT}")
# ===== ⑪ 抓取报告（agent 机器可读）+ 自动衔接下载（治 m3u8 链接几小时过期）=====
shibai = [{'季': jn, '集': xi, '标题': t} for bk, jn, xi, t, key in ep_list if bk not in results]
report = {'show': name, 'mode': mode, '总集数': len(ep_list),
          '已抓取': len(results), '含m3u8': ok, '失败清单': shibai,
          '链接文件': OUT, '断点文件': state_path(), '自动下载': not ARGS.no_download}
try:
    with open(os.path.join(os.path.dirname(OUT), '抓取报告.json'), 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print('抓取报告已写入 抓取报告.json')
except Exception as e:
    print(f'  ⚠ 抓取报告写入失败：{e}')
if ok and not ARGS.no_download:
    dl = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ts_merge.py')
    print('\\n===== 自动衔接：调起下载器（趁链接新鲜下载，成品已存在的集自动跳过） =====')
    try:
        subprocess.call([sys.executable, dl])   # 用当前同一个 python 跑，跑完回到本脚本继续收尾
    except Exception as e:
        print(f'  ⚠ 自动调起下载器失败（可手动运行 ts_merge.py）：{e}')
print("===== 全部完成 =====")
try:
    page.quit()      # 跑完自动关掉爬虫浏览器：不留浏览器僵尸进程吃内存（曾因残留进程把机器卡死）
except Exception:
    pass
