# -*- coding: utf-8 -*-
"""
自动选集下载 · 长视频平台 —— 全自动指令版
（思路源自一个自用的全自动窗口化爬取脚本：搜索 → 自动选正片 → 抓全部季与集 m3u8 → 逐集下载 ts 分片合并成 mp4）

说明：这是脱敏后的学习参考代码。代码里用到的站点选择器、URL 占位一律抽象化了，
     「请把你的目标站点适配进来再用」。对着真实站点跑会报找不到元素，这在意料之中。

用法（手动在命令行/编辑器终端里运行，跑完自动结束，绝不后台常驻；关闭即停止，重跑自动续传）：
  python main.py --kw 某剧                     # 全部季与集（预约页自动跳过）
  python main.py --kw 某剧 --mode latest       # 每季最后一集
  python main.py --kw 某剧 --mode ep --ep 3    # 指定第3集
  python main.py --kw xx --mode update         # 增量更新：只补新增的集（断点/成品自动跳过）

铁律（运行边界）：本脚本只在你手动运行时执行本次任务——
  · 断点（m3u8结果_*.json）只用于"下次手动运行时"继续补，不会自动触发；
  · 补断点/重跑跳过已抓已下载的集，全部发生在你手动启动之后。
"""
import argparse
import importlib.util
import json
import os
import random
import re
import sys
import time
import traceback

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

from DrissionPage import ChromiumPage

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HOME = 'https://target-site.example.com/'   # ← 请改成你的目标站点首页

# ===== 复用下载器（解析m3u8/下载ts分片/合并/转mp4）=====
_spec = importlib.util.spec_from_file_location('dl', os.path.join(BASE_DIR, 'ts_merge.py'))
dl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dl)

# ===== 单实例锁：防止脚本残留/重复启动=====
# 背景：曾出现编辑器分离运行/后台挂着旧进程，导致"打开就在跑、两个脚本抢页面"。
# 现在：启动时发现有活着的旧实例直接退出并提示；正常结束/崩溃都会清掉锁（PID 死了自动视为过期）。
LOCK_FILE = os.path.join(BASE_DIR, os.path.splitext(os.path.basename(__file__))[0] + '.lock')


def _pid_alive(pid):
    """判断 PID 对应进程是否还活着（Windows 下不能用 os.kill(pid,0)，那会直接杀死进程！）"""
    try:
        import psutil
        return psutil.pid_exists(pid)
    except ImportError:
        pass
    if sys.platform == 'win32':
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            kernel32 = ctypes.windll.kernel32
            h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not h:
                return False                      # 打不开句柄 = 进程已不存在
            code = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(h, ctypes.byref(code))
            kernel32.CloseHandle(h)
            return code.value == 259              # STILL_ACTIVE
        except Exception:
            return True                           # 查不了就当还活着，保守拒绝双开
    try:
        os.kill(pid, 0)                           # 非 Windows：signal 0 只探测不杀
    except OSError as e:
        return e.errno != 3
    return True


def acquire_lock():
    """拿单实例锁：有活着的旧实例就拒绝对新实例放行。返回 True=本实例可继续"""
    if os.path.exists(LOCK_FILE):
        old_pid = None
        try:
            with open(LOCK_FILE, encoding='utf-8') as f:
                old_pid = int(f.read().strip())
        except Exception:
            old_pid = None
        if old_pid and _pid_alive(old_pid):
            print(f'检测到上一个实例（PID {old_pid}）仍在运行——'
                  f'可能是被编辑器分离/关窗口没真停，或上次任务还没跑完。')
            print('请先在任务管理器里结束该 python 进程，再重新运行本脚本；')
            print('两个实例同时跑会互相抢页面/抢断点文件，这次先不启动。')
            return False
        try:
            os.remove(LOCK_FILE)                  # 旧 PID 已死 = 过期锁，清掉
        except Exception:
            pass
    try:
        with open(LOCK_FILE, 'w', encoding='utf-8') as f:
            f.write(str(os.getpid()))
    except Exception as e:
        print(f'⚠ 锁文件写入失败（{e}），仍继续运行（可能无法防双开）')
    return True

def release_lock():
    try:
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
    except Exception:
        pass

class ShowCtx:
    """每个节目独立断点/链接文件：多节目同跑互不覆盖；重跑自动续传"""
    def __init__(self, name_):
        self.name = name_
        self.san = re.sub(r'[\\/:*?"<>|]', '_', name_)[:40]
        self.OUT = os.path.join(BASE_DIR, f'm3u8链接_{self.san}.txt')
        self.STATE = os.path.join(BASE_DIR, f'm3u8结果_{self.san}.json')
        self.results = {}

# ===== 全局运行状态（每次进一部节目都会重置）=====
page = None
play_tab = None
mode = '剧集'
found, grouped, ep_list, biaoqian_all, ctx = {}, {}, [], [], None

# ===== 选择器占位（脱敏抽象）=====
# 把原脚本里的私有类名换成通用占位：card-title / sub-title / ep-item-row / ep-cell / #ep-list / #search-input。
# 适配时改成你目标站点的真实类名即可。
SEL_TITLE_XP = '(card-title, sub-title)'        # 卡片两套标题，见下方 xpath 用法
XP_TITLES = ['xpath://**[**(@class,"**")]',
             'xpath://**[**(@class,"**")]']
XP_EP_CELL = 'xpath://**[**(@class,"**") and @**]'
XP_EP_ROW = 'xpath://**[**(@class,"**") and @**]'

# ===== ① 搜索卡片标注 =====
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
    tt = re.search(r'(纪录片|动漫|电影|电视剧|综艺|短剧|少儿)', kz)
    if tt:
        parts.append(tt.group(1))
    y = re.search(r'(?:19|20)\d{2}(?!\d)', kz)
    if y:
        parts.append(y.group(0))
    g = re.search(r'(更新至\s*\d+\s*集|全\s*\d+\s*集|已完结|\d+\s*集全|更新至\s*\d+\s*话)', kz)
    if g:
        parts.append(re.sub(r'\s+', '', g.group(1)))
    return ' · '.join(parts)


# 播大卡入口验证：播放按钮/选集格子必须在卡片自己范围内找——
# 全页搜"立即播放"会匹配到别的卡（在某剧实测：是"立即预约"预告卡，
# 全页搜到的是第三张正剧大卡的按钮，结果选却进了正剧页）
js_ka = r"""
const t = this;
let p = t.parentElement;
while (p && p !== document.body) {
  const others = [...p.querySelectorAll('span[class*="**"], p[class*="**"]')]
    .filter(x => x !== t);
  if (others.length) break;                  // 到含其他标题的容器 = 出卡片了
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


def build_items(tab, kw):
    """搜索结果构建可选条目：跳过预约/预告卡，返回 [(标题, 元素, 卡文字)]"""
    items = []
    hot_title = tab.ele('xpath://[**(@class,"**")]')
    if hot_title and (hot_title.text or '').strip():
        kakou = str(hot_title.run_js(js_ka) or '')
        rk = hot_title.run_js('return window.__kaEle || null') if kakou != '无入口' else None
        if rk is None:
            print('（播卡只有"预告"，没有正片入口，跳过不进列表）')
        else:
            items.append(('【播】' + hot_title.text.strip(), rk, card_txt(hot_title)))
    # 新版搜索页两套标题并存：正主各季是 span.card-title，底部"相关影视"是 p.sub-title
    biaoti = tab.eles('xpath://**[**(@class,"**")]') or []
    sums = tab.eles('xpath://**[**(@class,"**")]') or []
    hebing = list(sums) + list(biaoti)
    # 按搜索词过滤：标题里含搜索词的才是本尊/同系各季；一个都不含就退回全部
    benzun = [e for e in hebing if kw in ((e.attr('title') or e.text or '').strip())]
    if hebing and len(benzun) < len(hebing):
        print(f'（按"{kw}"过滤：{len(hebing)} 条里匹配 {len(benzun)} 条）')
    hot_name = (hot_title.text or '').strip() if hot_title else ''
    if not items or not items[0][0].startswith('【播】'):
        hot_name = ''                       # 播卡没进列表（预告被跳过）：不再按它去重
    n_yuyue = 0
    for e in (benzun or hebing):
        t = (e.attr('title') or e.text or '').strip()
        if not t or t == hot_name:
            continue
        kz = card_txt(e)
        if '立即预约' in kz and str(e.run_js(js_ka) or '') == '无入口':
            n_yuyue += 1                    # 预告卡：没有正片可抓，不进列表
            continue
        items.append((t, e, kz))
    if n_yuyue:
        print(f'（{n_yuyue} 张"预约"没有正片入口，已跳过）')
    return items


def do_search(tab, kw):
    """回首页重新搜索关键词，返回可选条目列表（等待时间随机，防固定节奏被识别）"""
    tab.get(HOME)
    sb = None
    for _ in range(5):               # 页面没开出来/搜索框没渲染完：轮询等，防直接崩
        sb = tab.ele('#**-input')
        if sb:
            break
        time.sleep(2)
    if sb is None:
        raise SystemExit('搜索框没找到，页面可能没加载出来，重跑一次脚本')
    sb.input(kw + '\n')
    time.sleep(random.uniform(6, 9))   # 等搜索结果页渲染完成
    print('当前页面：', tab.url)
    return build_items(tab, kw)


# ===== ② 正片入口探测 =====
# 优先找卡片上的选集格子"1"（链接直达第1集正片页）。
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


def enter_show(tab, ele, name):
    """进入播放页，返回 play_tab；入口探测异常降级成点标题，后面还有花絮页补救"""
    global play_tab
    try:
        how = str(ele.run_js(js_zp) or '')
        href = ele.run_js('return window.__zpHref || ""') or ''
        zp = ele.run_js('return window.__zpEle || null')
    except Exception:
        how, href, zp = '', '', None
    print('正片入口：', how or '（探测失败，降级点标题）')
    try:
        if href:
            tab.get(href)                        # 格子自带正片链接：直接访问，比点击稳
            play_tab = tab
        else:
            if zp:
                zp.click(by_js=True)
            else:
                ele.click(by_js=True)             # js 点击避免悬浮层遮挡
            try:
                tab.wait.new_tab(timeout=10)
                play_tab = tab.latest_tab
            except Exception:
                play_tab = tab     # 没开新标签页就是当前页跳转
        print(f'已进入【{name}】：', play_tab.url)
        return play_tab
    except Exception:
        print(f'⚠ 进入【{name}】失败：{traceback.format_exc()[-200:]}')
        return None


# ===== ③ 集数面板/标签工具（格子/合体两用法共用）=====
js_scroll = '''
(() => {
  const box = document.querySelector('#ep-list') ||
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
  return '锁定(短列表) .scroll-view | 条目=' + items.length +
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


# 滚动容器探测：从条目（格子或列表卡片）往上找第一个可滚动祖先（虚拟列表才有），
# 返回 [scrollTop, 总高, 视口高]；'no-scroll'=一屏全放下不用滚，'no-cell'=条目还没渲染
js_pos = r"""
const cell = [...document.querySelectorAll('div[class*="ep-cell"][title], div[class*="ep-item-row"][title]')]
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
# 面板定位：不认 id（面板 id 每个站点/每部剧都不同）——
# 从条目（格子或列表卡片）往上爬，爬到第一个含"数字段(1-30)"或"季标签"文字的祖先容器 = 分区面板
js_box = r"""
const cell = [...document.querySelectorAll('div[class*="ep-cell"][title], div[class*="ep-item-row"][title]')]
  .find(e => e.getClientRects().length > 0);
if (!cell) return 'no-cell';
const hasSeg = p => [...p.querySelectorAll('*')].some(e => /^\d+-\d+$/.test(e.textContent.trim()));
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
    """面板标签按文字特征分行（类名每个站点都不同不能认）：季文字/数字段/其他类型"""
    tt = json.loads(play_tab.run_js(js_tabs) or '{}')
    return tt.get('g1') or [], tt.get('g2') or [], tt.get('g3') or []


# 标签收集：优先认 **（实测动漫/电视剧标签行统一是它，角标等碎片会被挡在外面），
# 没有 ** 结构的页面退回全扫兜底；** 长串是父容器，被长度过滤天然排除
js_tabs = r"""
const box = window.__box;
if (!box) return '{}';
let nodes = [...box.querySelectorAll('[class*="**"]')]
  .filter(e => e.getClientRects().length > 0);
if (!nodes.length)
  nodes = [...box.querySelectorAll('*')].filter(e => e.getClientRects().length > 0);
const ts = nodes.map(e => e.textContent.trim())
  .filter(t => t && t.length <= 10 && !/^\d{1,3}$/.test(t)
             && !/^\d+-\d+-/.test(t)   // 排除连体段标签（"1-3031-36"是两个段挤一起，永远是点不到的脏标签）
             && !/^更多/.test(t) && t !== '相关推荐' && t !== '操控列表' && t !== '选集');
return JSON.stringify({
  g1: [...new Set(ts.filter(t => /第.{0,6}(季|部)|特别/.test(t)))],
  g2: [...new Set(ts.filter(t => /^\d+-\d+$/.test(t)))],
  g3: [...new Set(ts.filter(t => !/第.{0,6}(季|部)|特别/.test(t) && !/^\d+-\d+$/.test(t)))]
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
els[els.length - 1].dispatchEvent(       // 与 js_var_click 一致：裸 click 在 React 页面经常被拦截不生效，用事件派发
  new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
return 'ok';
"""
    js_v = r"""
const box = window.__box;
if (!box) return 'no-box';
const want = __WANT__;
const sel = [...box.querySelectorAll('[class*="**"],[class*="**"],[class*="**"]')]
  .filter(e => e.getClientRects().length > 0)
  .map(e => e.textContent.trim());
return sel.includes(want) ? '已选中' : '未选中';   // 选中态元素(如**)的文字=目标标签
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

    return biaqian()[1]


def shengcheng_ep_list():
    """**(标题->5元组) 按季分组生成 **：季序=页面标签顺序，每季独立从1编号
    ** 元素 = (断点键, 季标签, 季内序号, 标题, 切集键)——断点键"季#集号"防不同季的"第1集"撞车"""
    for tt, key5 in sorted(found.items(), key=lambda x: (x[1][0], x[1][1], x[1][2])):
        grouped.setdefault(key5[3] or '正片', []).append(tt)
    for jn, tts in grouped.items():
        for xi, tt in enumerate(tts, 1):
            ep_list.append((f'{jn}#{xi}', jn, xi, tt, tt))


# 等格子渲染（上次只等5秒面板没出来抓了个空）：轮询最多再等 6 x 3 秒
def you_zhengge():
    """页面上有没有"数字格子"（正片集数格子可见文字是纯数字；花絮卡的文字是长标题）"""
    js_y = r"""return [...document.querySelectorAll('div[class*="**"][**]')]
  .some(e => e.getClientRects().length > 0 && /^\d{1,3}$/.test(e.textContent.trim()));"""
    return bool(play_tab.run_js(js_y))


def collect_eps():

    global mode, found, grouped, ep_list, biaoqian_all, play_tab
    found, grouped, ep_list = {}, {}, []
    time.sleep(5)       # 先等播放页渲染 5 秒左右，右侧集数面板才会出现
    jisu = []
    for _ in range(6):
        jisu = play_tab.eles(XP_EP_CELL)
        if jisu:
            break
        time.sleep(3)
    mode = '格子'
    yishou = set()
    if not you_zhengge() and not re.search(r'/[a-z]\d+\.html', play_tab.url):
        # 落在封面页了（URL ）：在页面里找"正片第1集"入口跳过去。
        # 优先找带链接的（a 标签直接访问最稳），没有链接就点数字格子"1"/"第1集"
        js_zheng = r"""
const here = location.href;
const a = [...document.querySelectorAll('a[href*="/play/"]')]
  .filter(x => x.href !== here && /\/play\/[^/]+\/[a-z]\d+\.html/.test(x.getAttribute('href') || ''))
  .filter(x => /^(第\s*0?1\s*[集话期]?|0?1)$/.test(x.textContent.trim()))[0];
if (a) return 'link|' + a.href;
const one = [...document.querySelectorAll('a,div,**,**')]
  .filter(x => !x.children.length && x.getClientRects().length > 0
             && /^(0?1|第\s*0?1\s*[集话期])$/.test(x.textContent.trim()))[0];
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
        jisu = play_tab.eles(XP_EP_CELL)
        print('补救后 URL：', play_tab.url)
    if jisu:
        # —— 格子模板分支（**同一套格子元素：可见文字是数字、** 是集标题或剧情描述）——
        #    多集多季的剧格子按分区标签储藏（"1-30"/"31-36"/季标签），**走下面剧集分支。
        #    流程 = 第一次获取（当前页全部，滚动触发懒加载）→ 逐个点标签 → 每停一处再抓一遍
        #           → 同名去重（后到覆盖先到，正确分区的排序位置盖掉兜底位置）→ 合并按序排
        print('集数列表滚动：', play_tab.run_js(js_scroll))
        time.sleep(2)
        def sazi(jx, dx, jn, dn):
            """统一抓取：抓当前渲染的全部条目；面板可滚动（合集虚拟列表）就边滚边抓到到底
            普通格子页一屏全放下，自动跳过滚动。分区内按首见顺序编号（虚拟列表的抓取顺序=展示顺序）"""
            seen, xu, dao, last, no_cell = set(), 0, 0, '', 0
            for lun in range(500):                 # 上限防死循环（一屏约10条，够2800+话的段用）
                for i, d in enumerate(play_tab.eles(XP_EP_CELL)):
                    tt = (d.attr('**') or '').strip()
                    if tt and not re.search(r'————', tt) and tt not in seen:
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
        if ding.startswith('锁定面板（有'):  # 真有标签才走分区循环；**页没标签防误点
            g1, g2, g3 = biaqian()
            jis = g1
            biaoqian_all = list(dict.fromkeys(g1 + g2 + g3))
            print('分区标签：=', jis, '| 段=', g2 or '（无）', '| 类型=', g3 or '（无）')
            if jis:
                for jx, j in enumerate(jis):
                    dian(j)
                    for dx, d in enumerate(duan_now() or [None]):
                        if d:
                            dian(d)
                        sazi(jx, dx, j, d)
            elif g2:
                for dx, d in enumerate(g2):
                    dian(d)
                    sazi(0, dx, None, d)
            elif g3:
                for dx, d in enumerate(g3):
                    dian(d)
                    sazi(0, dx, None, d)
        else:
            print('（无分区面板，当前页就是全部）')
        print(f'去重合并后共 {len(found)} 条')
        shengcheng_ep_list()
    else:
        # —— 先探测合体：无格子，但列表条目+ 分区标签（格子式）同时存在 = 合集页 ——
        #    如某完结合集：条目是列表卡片且虚拟滚动，但按"**"分区储藏
        liebiao = []
        for _ in range(4):                  # 列表条目还没渲染就等，最多 4 x 3 秒
            liebiao = play_tab.eles(XP_EP_ROW)
            if liebiao:
                break
            time.sleep(3)
        ding = str(play_tab.run_js(js_box) or '')
        if liebiao and ding.startswith('锁定面板（有'):
            mode = '合体'
            print('分区面板：', ding, '—— 列表条目+分区标签 = 合体用法')
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
                        if t and t not in seen and not re.search(r'--', t):
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
            if g1:
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
            # —— 剧集通用分支：**都是这套元素（虚拟列表），只是列表长短不同 ——
            #    长列表（**47集/**38集）：虚拟列表懒加载，滚轮往上顶分批补加载，两阶段收集
            #    短列表（**3条版本）：一屏渲染完、容器不可滚动，锁定后直接抓
            mode = '剧集'
            yishou = set()
            dingwei = play_tab.run_js(js_lock)
            for _ in range(3):                  # 还没渲染出来就继续等，最多再等 3 轮 x 3 秒
                if not str(dingwei).startswith('no'):
                    break
                time.sleep(3)
                dingwei = play_tab.run_js(js_lock)
            print('列表定位：', dingwei)
            fnd = {}    # 标题 -> 列表内绝对位置（同一坐标系，排序才准）
            if '短列表' in str(dingwei):        # **类：不用滚轮，这一屏就是全部
                for t, pos in snapshot():
                    fnd[t] = pos
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
                    for t, pos in snapshot():
                        fnd[t] = pos          # 内容不再增减，反复覆盖值也一样
                    wheel(play_tab, 300)        # 往下滚一屏
                    time.sleep(0.6)
                    for t, pos in snapshot():
                        fnd[t] = pos
                    kong = kong + 1 if len(fnd) == before else 0
                    before = len(fnd)
                    if kong >= 3:               # 连续3轮没新内容 = 到底了
                        break
            for t, xu in sorted(fnd.items(), key=lambda x: x[1]):   # 第1期/第1条在最上
                xi = len(ep_list) + 1
                ep_list.append((f'正片#{xi}', '正片', xi, t, t))
    print(f'\n共 {len(ep_list)} 集（{mode}流程），按**分组如下：')
    now_j = None
    for bk, jn, xi, t, key in ep_list:
        if jn != now_j:                     # 换**了：打一行**标题
            now_j = jn
            cnt = sum(1 for b2, j2, _, _, _ in ep_list if j2 == jn)
            print(f'\n【{jn}】共 {cnt} 集：')
        print(' ', xi, '|', t)
    if not ep_list:
        print('⚠ 未找到集数列表，请检查页面是否正常加载')
        return set()
    if not (mode == '格子' and len(ep_list) >= 10) and \
            not [tt for _, _, _, tt, _ in ep_list if re.search(r'第\d+(集|话|期)', tt)]:
        # 同一部剧"正片入口"有集数面板、"花絮入口"只有花絮列表（如某剧第二季两种布局）。
        # 格子流程抓到10条以上就当正片——某剧的格子标题是剧情描述不带"第N集"字样，
        # 而花絮/预告列表一般不到10条；列表里一条正剧格式都没有 = 八成点进了花絮/预告页
        print('⚠ 列表里没有"第N集/话/期"格式的正剧条目——这个入口八成是花絮/预告页，')
        print('  这条卡会跳过不下载（换搜索结果里的其他条目再跑；或搜完整剧名如"某剧第二季"）')
        return set()
    return {jn for _, jn, _, _, _ in ep_list}


# ===== ④ 抓 URL 公共函数（** 甄别）=====
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
    """从网络包里递归找出 **（清晰度/播放地址的官方数据结构）"""
    try:
        body = packet.response.body
        if isinstance(body, (bytes, bytearray)):
            body = body.decode('utf-8', errors='ignore')
        data = body if isinstance(body, dict) else json.loads(body)
    except Exception:
        return None
    raw = find_key(data, '**')
    if raw is None:
        return None
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return None


def capture_vinfo(tab, seconds=20):
    """在 seconds 秒内甄别网络包，抓到 ** 就返回；返回 (**, 甄别包数)"""
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
        tab.listen.start('**') # 监听 ** 字段网络请求包
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


def collect_m3u8(obj, ctx_h, out):
    """递归遍历 **，收集所有 m3u8 地址及其所在层的清晰度线索（字段名不固定也能关联上）"""
    if isinstance(obj, dict):
        hints = dict(ctx_h)
        for k in ('cname', 'clarity', 'definition', 'resolution', 'name'):
            v = obj.get(k)
            if isinstance(v, (str, int)) and str(v):
                hints.setdefault(k, str(v))
        if obj.get('**'):                      # vh=视频高度，如 1080
            hints['**'] = obj['**']
        m = re.search(r'\.f(\d{4,})\.', str(obj.get('fn') or ''))   # fn 形如 xxx.f322464.ts，内含档位id
        if m:
            hints['fid'] = m.group(1)
        for v in obj.values():
            collect_m3u8(v, hints, out)
    elif isinstance(obj, list):
        for v in obj:
            collect_m3u8(v, ctx_h, out)
    elif isinstance(obj, str) and obj.startswith('http') and '.m3u8' in obj:
        cname = ctx_h.get('cname') or ctx_h.get('clarity') or ctx_h.get('definition') \
            or ctx_h.get('resolution') or ctx_h.get('name') or ''
        m = re.search(r'\.f(\d{4,})\.', obj)     # URL 本身也带档位id：xxx.f322464.ts.m3u8
        out.append({'name': cname, '**': ctx_h.get('**') or 0,
                    'fid': ctx_h.get('fid') or (m.group(1) if m else ''), 'url': obj.strip('`')})


def pick_m3u8(vinfo):
    """挑出清晰度最高的 m3u8，返回 (清晰度名, 地址, 全部清晰度列表)"""
    # 官方清晰度表：** 里 档位id -> (名称, 高度)，如 322464 -> (如'高清SDR;(1080P)', 804)
    fi_map = {}
    for fi in (vinfo.get('**') or {}).get('**') or []:
        fi_map[str(fi.get('id'))] = (str(fi.get('cname') or fi.get('sname')
                                      or fi.get('resolution') or ''), fi.get('height') or 0)
    found = []
    collect_m3u8(vinfo, {}, found)
    seen, uniq = set(), []
    for it in found:                   # 同一地址可能出现多次，去重
        if it['url'] not in seen:
            seen.add(it['url'])
            uniq.append(it)
    if not uniq:                       # 最后兜底：直接在 vinfo 文本里搜
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
        if height and not it.get('**'):
            it['**'] = height
        key = fid or ('x_' + it['url'])    # 没有档位id的（如字幕）按地址算
        if key in by_fid:
            continue
        by_fid[key] = it
        order.append(it)
    order.sort(key=lambda x: ((x['**'] if isinstance(x['**'], (int, float)) else 0),
                              _clarity_score(x['name'])), reverse=True)
    best = order[0]
    return best['name'] or (f"{best['**']}P" if best['**'] else '未知'), best['url'], order


# ===== ⑤ 切集点击：统一入口，内部分流 =====
js_var_click = """
const want = __WANT__;
const b = window.__list || window.__box;
if (!b) return 'no-box';
for (const it of b.querySelectorAll('div[class*="**"][**]')) {   // **列表
  if (it.getAttribute('**') === want) {
    const card = it.closest('div[class*="**"]') || **;
    card.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
    return 'clicked';
  }
}
const cands = [...b.querySelectorAll('div[class*="**"]')];         // 电视剧网格格子
let hit = cands.find(e => e.getAttribute('**') === want)
       || cands.find(e => e.textContent.trim() === want);
if (!hit && /^\\d{1,3}$/.test(want))     // 文字格式对不上时按数值比（"1"vs"01"）
  hit = cands.find(e => /^\\d{1,3}$/.test(e.textContent.trim())
                     && parseInt(e.textContent.trim(), 10) === parseInt(want, 10));
if (hit) {
  hit.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
  return 'clicked';
}
return '**';
"""


def click_ep(tab, key):
    """统一切集入口：格子/剧集模板的 key 都是条目标题
    格子模板：面板整批换格子，该集在哪个分区就先照原路点回去（季标签→数字段标签）再点格子；
    剧集模板：虚拟列表会卸载滚出视口的卡片，先用记录的位置滚到目标居中等 1 秒渲染再点"""
    global found, play_tab
    if mode in ('格子', '合体'):
        info = found.get(key)
        if info and (info[3] or info[4]):    # 该集属于某个分区：先把面板点回去
            if info[3]:
                dian(info[3])
            if info[4]:
                dian(info[4])
        tab.run_js(js_box)                   # 面板现锁一遍：上一集点击可能刚引发翻页导航，页面上旧的__box已死
        for _ in range(3):          # 切集后面板可能重渲染空窗：锁不到就等1秒重锁，避免拿旧DOM白找
            bx = str(play_tab.run_js(js_box) or '')
            if bx.startswith('锁定'):
                break
            time.sleep(1)
        tab.run_js("window.__list = window.__box; return 'ok';")
        # 回切时列表滚动条可能停在中间，而查找循环只往下滚，
        # 目标在最顶上会被永远漏掉：先把列表滚回顶部再开始找。
        tab.run_js("const b = window.__list; if (b) b.scrollTop = 0; return 'ok';")
        # 点击容器先强制=面板：防止 js_pos 抢先把 __list 锁到别的模块 抢走 js_var_click 的优先级；
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


# ===== ⑥ 断点存档（带节目名防拿错）=====
def save_state(c):
    """断点存档：每抓到一集就写 json，重跑脚本自动跳过已抓的集，避免重复请求"""
    try:
        with open(c.STATE, 'w', encoding='utf-8') as f:
            json.dump({'show': c.name, 'eps': c.results}, f, ensure_ascii=False, indent=1)
    except Exception as e:
        print(f"  ⚠ 断点存档失败：{e}")


def load_results(c):
    """启动时载入上次断点，已抓过的集不再重复请求；节目对不上就不用它的断点"""
    if not os.path.exists(c.STATE):
        return
    try:
        with open(c.STATE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict) and 'eps' in data:     # 新结构（带节目名）
            if data.get('show') != c.name:
                print(f"  断点是《{data.get('show')}》的，和当前节目不同，忽略从头抓")
                return
            data = data['eps']
        else:
            print('  旧版断点格式，无法确认节目，忽略从头抓')
            return
        c.results.update(data)          # 键是"季#集号"字符串，直接沿用
        print(f"  已载入断点：之前已抓 {len(c.results)} 集，将自动跳过")
    except Exception as e:
        print(f"  ⚠ 断点文件读取失败（忽略，从头抓）：{e}")


def save_txt(c):
    """每抓到一集就整体重写文件（按季分组），中途崩溃也不丢已抓结果"""
    tmp = c.OUT + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            now_j = None
            for bk, jn, xi, t, key in ep_list:
                r = c.results.get(bk)
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
        os.replace(tmp, c.OUT)       # 先写临时文件再原子替换：文件被占用时报错但不会写坏旧内容
    except PermissionError:
        print("  ⚠ m3u8链接文件被占用（可能正被别的程序打开），本集结果已存断点，下集会再尝试写入")


def save_one(c, bk, jn, xi, title, vinfo):
    nm, m3u8, all_q = pick_m3u8(vinfo)
    c.results[bk] = {'title': title, 'name': nm, 'm3u8': m3u8, 'all': all_q}
    extra = f"，共 {len(all_q)} 种清晰度" if len(all_q) > 1 else ''
    print(f"✅ 【{jn}】第{xi}集（{title}）[{nm}]{extra} {m3u8[:120]}")
    save_txt(c)
    save_state(c)


# ===== ⑦ 抓单集全链路（单集验证和批量抓取共用）=====
def fetch_one(c, bk, jn, xi, title, key, seconds=15):
    """切到指定集 -> 监听网络包 -> 提取 ** -> 挑最高清晰度存档。抓到返回 True"""
    global play_tab
    start_listen(play_tab)            # 清掉上一集的包，只留本次切集后的
    if not click_ep(play_tab, key):
        print(f"⚠ 点击【{jn}】第{xi}集失败，跳过")
        return False
    print(f"  已点击【{jn}】第{xi}集（{title}），等待 ** ...")
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
        save_one(c, bk, jn, xi, title, vinfo)
        return True
    print(f"⚠ 【{jn}】第{xi}集（{title}）{seconds}秒内未抓到**（甄别 {cnt} 个包）")
    return False


def warmup_first(c, first_ep):
    global play_tab
    bk0, jn0, xi0, t0, _ = first_ep
    if len(ep_list) > 1 and click_ep(play_tab, ep_list[1][4]):
        if len(page.tab_ids) > 1 and page.latest_tab.tab_id != play_tab.tab_id:
            print(f"  预热回退方案不可用，改用点位抓取头部...")
            fetch_one(c, *first_ep, seconds=20)
        else:
            print(f"  先切到第2个（{ep_list[1][3]}）预热，等3秒...")
            time.sleep(3)
            # 回头部播放页保底方案：历史后退 = 整页重载会重新发 ** 请求
            start_listen(play_tab)
            try:
                play_tab.back()
            except Exception:
                play_tab.run_js('history.back()')
            time.sleep(1)
            vinfo, _cnt = capture_vinfo(play_tab, seconds=10)
            if vinfo:
                print(f"  （回退回到第{xi0}集，已抓回 **）")
                save_one(c, bk0, jn0, xi0, t0, vinfo)
            else:
                print("  回退未抓到 **，退回点位抓取...")
                fetch_one(c, *first_ep, seconds=20)
    else:
        fetch_one(c, *first_ep, seconds=20)


# ===== ⑧ 逐集下载合并（抓完立刻下，链接有时效）=====
def _label_of(jn, xi):
    """带季名的下载标签：多季剧集号会重名，标签带上季名"""
    seasons = {b[1] for b in ep_list}
    multi = len(seasons) > 1
    return f'{jn}第{xi}集' if multi else f'第{xi}集'


def _safe_name(ctx, jn, xi, title):
    return re.sub(r'[\\/:*?"<>|]', '_', f'{ctx.san}_{_label_of(jn, xi)}_{title}')[:60]


def _has_mp4(ctx, jn, xi, t):
    return os.path.exists(os.path.join(BASE_DIR, _safe_name(ctx, jn, xi, t) + '.mp4'))


def download_ep(ctx, e, rec):
    """单集下载：解析 m3u8（主链接失效自动换备选）→ 下载 ts 分片 → 合并 → 转 mp4。
    成品已存在自动跳过；链接过期会提示（m3u8 链接有时效，过期需删 _json 文件重抓）"""
    jn, xi, t = e[1], e[2], e[3]
    safe = _safe_name(ctx, jn, xi, t)
    out_base = os.path.join(BASE_DIR, safe)
    if os.path.exists(out_base + '.mp4') or os.path.exists(out_base + '.ts'):
        if os.path.exists(out_base + '.mp4'):
            print(f'\n【{jn}】第{xi}集（{t}）已有 mp4，跳过下载')
        else:
            print(f'\n【{jn}】第{xi}集（{t}）已有 .ts（未转 mp4），直接转封装')
            dl.to_mp4(out_base + '.ts')
        return 'skip'
    print(f'\n===== 下载【{jn}】第{xi}集（{t}）=====')
    mirrors = [rec['m3u8']] + [q['url'] for q in rec.get('all') or []][1:]
    text = real = segs = None
    for u in mirrors:
        try:
            text, real = dl.load_playlist(u)
            segs = dl.parse_segments(text, real)
            if segs:
                break
        except Exception as ex:
            print(f'  链接不可用：{str(ex)[:60]}')
            text = None
    if text is None or not segs:
        print(f'⚠ 【{jn}】第{xi}集所有链接都失效（m3u8 链接有时效，一般几小时）。'
              f'重跑抓取脚本可换新链接（断点里该集已有不会自动刷新，需删 {os.path.basename(ctx.STATE)} 重抓）')
        return 'fail'
    print(f'  解析出 {len(segs)} 个分片')
    key_info = None
    for line in text.splitlines():
        if line.startswith('#EXT-X-KEY'):
            key_info = dl.parse_key(line, real)
            break
    seg_dir = os.path.join(dl.SEG_DIR, safe)
    fail = dl.download_segments(segs, dl.decrypt_factory(key_info), seg_dir)
    if fail:
        print(f'失败 {len(fail)} 个：分片 {fail[:20]}{"..." if len(fail) > 20 else ""}')
    dl.to_mp4(dl.merge_segments(len(segs), seg_dir, out_base), seg_dir)
    return 'ok'


def process_eps(c, want):
    """逐集抓 m3u8 并立即下载合并（抓完就下，链接有时效）；断点/成品自动跳过。
    返回处理的集数"""
    global play_tab
    n = 0
    first_ep = ep_list[0]
    # 单集验证/预热：重开页面时正在播的正好是第1集，先切走再回退才能逼出新的 ** 请求
    if want and want[0][0] == first_ep[0] and first_ep[0] not in c.results:
        warmup_first(c, first_ep)
    for e in want:
        bk, jn, xi, t, key = e
        if _has_mp4(ctx, jn, xi, t):
            print(f'\n【{jn}】第{xi}集（{t}）已有 mp4 成品，跳过')
            n += 1
            continue
        if os.path.exists(os.path.join(BASE_DIR, _safe_name(ctx, jn, xi, t) + '.ts')):
            print(f'\n【{jn}】第{xi}集（{t}）有 .ts 半成品，补转 mp4')
            try:
                download_ep(ctx, e, {})
                n += 1
            except Exception:
                print(f"⚠ 【{jn}】第{xi}集补转封装出错：{traceback.format_exc()[-200:]}")
            continue
        if bk not in ctx.results:
            try:
                ok = fetch_one(c, bk, jn, xi, t, key)
            except Exception:
                print(f"⚠ 【{jn}】第{xi}集处理出错，跳过本集继续：")
                traceback.print_exc()
                ok = False
            if not ok:
                print(f'  （{jn} 第{xi}集没抓到 **，本集放弃，重跑会自动跳过已抓到的）')
                time.sleep(random.uniform(5, 8))
                continue
        rec = c.results.get(bk)
        if rec:
            try:
                download_ep(c, e, rec)
                n += 1
            except Exception:
                print(f"⚠ 【{jn}】第{xi}集下载出错：{traceback.format_exc()[-200:]}")
            time.sleep(random.uniform(1.5, 3))
        else:
            print(f'  （跳过下载：断点里没有 {jn} 第{xi}集 的 m3u8）')
        time.sleep(random.uniform(2, 4))   # 模拟人工切集节奏，防请求过密
    save_txt(c)
    ok = sum(1 for v in c.results.values() if v.get('m3u8'))
    print(f'\n《{c.name}》应处理 {len(want)} 集：断点含 {len(c.results)} 集（m3u8 有效 {ok} 集），结果已存 {c.OUT}')
    return n


# ===== ⑨ 模式筛选（全部/最新一集/指定单集/增量更新 共用）=====
def pick_want(mode_, ep_n):
    """从 ** 选出本次要抓的集；返回 ** 元素列表"""
    if mode_ in ('all', 'update'):
        return list(ep_list)
    if mode_ == 'latest':            # 每季最后一集
        best = {}
        for e in ep_list:
            jn = e[1]
            if jn not in best or e[2] > best[jn][2]:
                best[jn] = e
        return [best[jn] for jn in best]
    # ep：指定第N集（多季时按季序取第一个）
    hits = [e for e in ep_list if e[2] == ep_n]
    if not hits:
        hits = [e for e in ep_list if re.search(rf'第\s*{ep_n}\s*[集话期]', e[3])]
    return hits[:1]


def pick_item(items, title0, idx0):
    """重新搜索后按标题定位目标卡片；标题对不上退回原序号"""
    for it in items:
        if it[0] == title0:
            return it
    try:
        return items[idx0]
    except IndexError:
        return None


def back_home(tab):
    """收尾：关掉所有播放标签页，回到目标站点首页（下一条节目从搜索重新进）"""
    try:
        for tl in list(tab.tab_ids):
            if tl != tab.tab_id:
                try:
                    tab.close_tabs(tl)
                except Exception:
                    pass
                time.sleep(0.4)
    except Exception:
        pass
    try:
        tab.get(HOME)
    except Exception:
        pass


def run_one_show(kw, title0, idx0, covered):
    """处理一个节目：搜索定位→进播放页→收集全部季与集→按模式抓m3u8并逐集下载。返回处理集数"""
    global page, play_tab, ctx
    items = do_search(page, kw)
    item = pick_item(items, title0, idx0)
    if item is None:
        print(f'（重新搜索未找到卡片：{title0}，跳过）')
        return 0
    t, ele, _ = item
    play_tab = enter_show(page, ele, t)
    if play_tab is None:
        return 0
    jn_set = collect_eps()
    if not jn_set:
        print(f'（{t} 没抓到集数列表，可能是其他播放内容，跳过）')
        return 0
    covered.update(jn_set)
    want = pick_want(args_mode, args_ep)
    if not want:
        print(f'（{t} 按模式 [{args_mode}] 没选出要下的集）')
        return 0
    ctx = ShowCtx(t)
    load_results(ctx)
    n = process_eps(ctx, want)
    return n


# ===== ⑩ 主流程 =====
args_mode = 'all'
args_ep = None


def main():
    global page, args_mode, args_ep
    ap = argparse.ArgumentParser(
        description='自动选集下载：搜索→自动选正片→抓全部季与集 m3u8→逐集下载合并成 mp4',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='示例：\n'
               '  python main.py --kw 某剧\n'
               '  python main.py --kw 某剧 --mode latest\n'
               '  python main.py --kw 某剧 --mode ep --ep 3\n'
               '  python main.py --kw xx --mode update\n')
    ap.add_argument('--kw', required=True, help='搜索关键词，如：某剧名')
    ap.add_argument('--mode', default='all', choices=['all', 'latest', 'ep', 'update'],
                    help='all=全部季与集；latest=每季最后一集；ep=指定第N集；update=增量更新新增集')
    ap.add_argument('--ep', type=int, default=None, help='--mode ep 时指定集数，如 --ep 3')
    args = ap.parse_args()
    if args.mode == 'ep' and not args.ep:
        sys.exit('--mode ep 需要 --ep N 指定集数，例如：--mode ep --ep 3')
    args_mode, args_ep = args.mode, args.ep

    if not acquire_lock():
        sys.exit(0)                       # 有活着的旧实例：拒绝双开
    print('提示：本脚本全程窗口化运行，跑完/出错/中断都会自动关闭爬虫浏览器；单实例锁已启用，重复启动会直接退出。')
    print('运行边界：本次任务只在当前运行期间执行；关闭后立即结束，断点留到下次手动运行时再补。你随时可以 Ctrl+C。')

    try:
        page = ChromiumPage()
    except Exception as e:
        release_lock()
        sys.exit(f'浏览器启动失败：{e}')

    try:
        time.sleep(1)
        first = do_search(page, args.kw)
        print(f'\n可进入的节目共 {len(first)} 个：')
        for i, (t, _, kz) in enumerate(first, 1):
            bz = biaozhu(kz)
            print(i, '|', t + (f'（{bz}）' if bz else '（类型未知）'))
        if not first:
            raise SystemExit('搜索结果里没有可进入的节目（可能全是**，或页面还没加载出来）')
        covered = set()              # 已覆盖的季标签集合（"**"）
        total = 0
        for idx0, (title0, _, _) in enumerate(first):
            print(f'\n########## [{idx0 + 1}/{len(first)}] 处理节目：{title0} ##########')
            try:
                m = re.search(r'第[一二三四五六七八九十百\d]+季', title0)
                if m and m.group(0) in covered:
                    print(f'（{title0} 的{m.group(0)}已在前面抓过，跳过）')
                    continue
            except Exception:
                pass
            try:
                total += run_one_show(args.kw, title0, idx0, covered)
            except Exception:
                print(f'⚠ 处理 {title0} 时出错：{traceback.format_exc()[-300:]}')
            back_home(page)
            time.sleep(random.uniform(3, 6))
        print(f'\n===== 全部完成：共处理约 {total} 集 =====')
    except KeyboardInterrupt:
        print('\n用户中断，已停止（已完成的集都已存盘/存断点，重跑会自动跳过）')
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
    finally:
        try:
            page.quit()      # 跑完自动关掉浏览器：不留僵尸进程吃内存（卡死教训）
        except Exception:
            pass
        release_lock()       # 正常/中断/崩溃都释放单实例锁
        print('浏览器已关闭，脚本结束')


if __name__ == '__main__':
    main()