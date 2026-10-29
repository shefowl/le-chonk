"""Build the Le Chonk control mod (mod/le_chonk) from the tables below.

    python3 mod/build.py

The panel: a top bar button opens a command desk with six sections.
- Agents: who plays what, a checkbox per agent and its last turn.
- Staff: the chain of command as a tree; a click on an agent shows what it decides,
  what it does not touch and whom it answers to.
- Theatres: the modes for theatres and fleets, and the theatres under our marshals.
- Reports: the feed: one line per report or event, a coloured rank badge per agent
  (written by the harness with the game's own colour byte 0x11), a filter per agent and
  "important only".
- The agent window (no tab button; "Open" on a Staff card): its standing directives, the
  chat with it or its own reports, the input box, Send and "Report".
- Plan: the plan's idea, its wars and their readiness (read only).

The localisation contains fixed-byte human defaults with zero-width palette signatures,
found in memory and overwritten in place after full hash validation (harness/modui.py). Buttons set country
flags and bump lc_dirty; the harness reads them through the console.
"""
import os
import re
import sys
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "le_chonk")


def _roster():
    """harness/roster.py's tables (SETTINGS, PAUSE_AGENTS ...), so the two never drift."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "lc_roster", os.path.join(HERE, os.pardir, "harness", "roster.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


ROSTER = _roster()
STEP = {"supreme_every": 5, "staff_every": 5}     # days a click moves these by

AGENTS = [("planner", "Planner", "Планирование"),
          ("supreme", "Supreme command", "Ставка"),
          ("events", "Events & diplomacy", "События и дипломатия"),
          ("politics", "Politics", "Политика"),
          ("industry", "Industry & research", "Промышленность и наука"),
          ("army", "Army staff", "Армейский штаб"),
          ("air", "Air staff", "Штаб ВВС"),
          ("admiralty", "Admiralty", "Адмиралтейство")]
TABS = [("agents", "AGENTS", "АГЕНТЫ"), ("staff", "STAFF", "ШТАБ"),
        ("theatres", "THEATRES", "ТЕАТРЫ"),
        ("reports", "REPORTS", "ДОНЕСЕНИЯ"), ("plan", "PLAN", "ПЛАН"),
        ("settings", "SETTINGS", "НАСТРОЙКИ")]
N_THEATRES, N_FEED, N_CHAT, N_DIRECTIVES, N_WARS = 8, 13, 11, 3, 3

# the chain of command as it is in the harness (harness/agents.py, admiralty.py,
# planner.py): (key, depth, english name, russian name). The marshals have no switch:
# the Theatres tab says which theatres are theirs.
STAFF = [("planner", 0, "Planner (your level)", "Планирование (ваш уровень)"),
         ("supreme", 1, "Supreme command", "Ставка"),
         ("events", 2, "Events & requests", "События и запросы"),
         ("politics", 2, "Politics", "Политика"),
         ("industry", 2, "Industry & research", "Промышленность и наука"),
         ("army", 2, "Army staff", "Армейский штаб"),
         ("air", 2, "Air staff", "Штаб ВВС"),
         ("admiralty", 2, "Admiralty", "Адмиралтейство"),
         ("marshals", 2, "Field marshals", "Фельдмаршалы")]
# the agents with a window of their own (chat, directives, report); the events turns
# are Supreme command’s, and the marshals get theirs per theatre later
CHAT = ("planner", "supreme", "politics", "industry", "army", "air", "admiralty")
# the colour of each agent's badge in the feed (game text colours, interface/core.gfx)
BADGE = {"planner": "C", "supreme": "Y", "events": "8", "politics": "L", "industry": "7",
         "army": "9", "air": "4", "admiralty": "2", "marshals": "6"}

# what each agent decides, what it leaves alone, whom it answers to: written from its
# tools; change it when an agent gets or loses a tool
CARDS = {
    "planner": (
        "The strategic plan: the idea of the war, whom to fight and in what order, when each "
        "war is ready and the date to start it by. Reads the HoI4 wiki and the web.",
        "Gives no orders: not to troops, factories or diplomats. Only the plan.",
        "The top. Supreme command follows the plan. When you play yourself this is your level: "
        "untick the planner and give your directives to the ministries and the marshals.",
        "Стратегический план: замысел войны, с кем и в каком порядке воевать, когда каждая "
        "война готова и до какой даты её начать. Читает вики HoI4 и интернет.",
        "Приказов не отдаёт: ни войскам, ни заводам, ни дипломатам. Только план.",
        "Высший уровень. Ставка следует плану. Если вы играете сами, это ваш уровень: "
        "снимите галочку и давайте директивы министерствам и маршалам напрямую."),
    "supreme": (
        "Armies and army groups, generals and field marshals, the theatres and their zones, "
        "a directive for each theatre. Diplomacy: war goals, declarations of war, factions, "
        "guarantees, lend-lease, calls to allies.",
        "Does not lead the battle inside a theatre (the marshals do). Does not produce, "
        "research or build: orders it from Industry. Focuses and laws: orders Politics. "
        "The air war: orders the air staff.",
        "Follows the plan (or you). Orders the ministries and the staffs, writes to the "
        "marshals.",
        "Армии и группы армий, генералы и фельдмаршалы, театры и их зоны, директива каждому "
        "театру. Дипломатия: цели войны, объявление войны, фракции, гарантии, ленд-лиз, "
        "призыв союзников.",
        "Не ведёт бой внутри театра (это маршалы). Не производит, не исследует и не строит: "
        "приказывает промышленности. Фокусы и законы: приказывает политике. "
        "Авиация: приказывает штабу ВВС.",
        "Следует плану (или вам). Отдаёт приказы министерствам и штабам, пишет маршалам."),
    "events": (
        "Supreme command between its strategic turns: answers the game's events and requests at "
        "once, so they do not wait for the next turn.",
        "Nothing else: no orders, no diplomacy of its own.",
        "The supreme command, in short event-handling turns.",
        "Ставка между стратегическими ходами: сразу отвечает на события игры и запросы, "
        "чтобы они не ждали следующего хода.",
        "Больше ничего: ни приказов, ни своей дипломатии.",
        "Та же ставка, короткими ходами по событиям."),
    "politics": (
        "The national focus, laws, advisers and design companies, decisions and missions. "
        "Intelligence: the agency and its upgrades, operatives, networks.",
        "No diplomacy and no wars (Supreme command). No production or construction.",
        "Carries out Supreme command’s orders and your directives.",
        "Национальный фокус, законы, советники и конструкторские бюро, решения и миссии. "
        "Разведка: агентство и его улучшения, агенты, сети.",
        "Ни дипломатии, ни войн (это ставка). Ни производства, ни стройки.",
        "Выполняет приказы ставки и ваши директивы."),
    "industry": (
        "Research, production lines of land and air equipment, construction and its "
        "order, tank and other designs, buying resources.",
        "Does not raise or move troops. Shipyards are the Admiralty's. Equips the "
        "divisions first, then the orders of the air staff.",
        "Supreme command’s orders, the air staff's aircraft orders, your directives.",
        "Исследования, производственные линии сухопутной и авиатехники, стройка и её "
        "очерёдность, проекты танков и прочей техники, покупка ресурсов.",
        "Не формирует и не двигает войска. Верфи принадлежат адмиралтейству. Сначала "
        "снаряжает дивизии, потом заказы штаба ВВС.",
        "Приказы ставки, заказы штаба ВВС на самолёты, ваши директивы."),
    "army": (
        "Land doctrine, division templates, training and exercises, motorized supply hubs. "
        "On Supreme command’s order it forms armies and shifts divisions between them.",
        "Does not lead battles (the marshals), does not draw theatres (Supreme command), "
        "does not fly the air force (the air staff).",
        "Supreme command; reports the army's needs to it.",
        "Сухопутная доктрина, шаблоны дивизий, подготовка и учения, моторизация "
        "узлов снабжения. По приказу ставки формирует армии и перебрасывает дивизии.",
        "Не ведёт бой (это маршалы), не нарезает театры (это ставка), не командует "
        "авиацией (это штаб ВВС).",
        "Ставка; докладывает ей нужды армии."),
    "air": (
        "The whole air war: fighters and close air support over the battles (wings "
        "attached to the attacking armies), interception, strategic and naval bombing, "
        "air bases. Air doctrine, aircraft designs, aircraft orders to Industry.",
        "Carrier wings are the Admiralty's. Does not lead the ground battle.",
        "Supreme command and your directives; an attack that begins wakes it.",
        "Вся воздушная война: истребители и штурмовики над боями (крылья, приданные "
        "наступающим армиям), перехват, стратегические и морские бомбардировки, "
        "аэродромы. Доктрина ВВС, проекты самолётов, заказы самолётов промышленности.",
        "Палубная авиация принадлежит адмиралтейству. Наземный бой не ведёт.",
        "Ставка и ваши директивы; начало наступления будит его сразу."),
    "admiralty": (
        "The whole navy: fleets and task forces, their missions and regions, home bases, "
        "admirals and the naval HQ, ship designs and the shipyards, repairs, convoys, "
        "carrier wings, naval projects and doctrine, requests for naval bases.",
        "No land or air war. Landings are the marshals': the Admiralty sees them and "
        "keeps the sea for them.",
        "Supreme command and your directives.",
        "Весь флот: флоты и соединения, их задачи и районы, базы, адмиралы и морской штаб, "
        "проекты кораблей и верфи, ремонт, конвои, палубная авиация, морские проекты и "
        "доктрина, запросы на строительство баз.",
        "Не воюет на суше и в воздухе. Высадки ведут маршалы, адмиралтейство их видит и "
        "держит для них море.",
        "Ставка и ваши директивы."),
    "marshals": (
        "One per theatre: attacks and pursuit, defence lines, garrisons, sectors between "
        "its armies, landings and paradrops, operations, the theatre plan.",
        "Does not change its zone (Supreme command draws it), raises no divisions, produces nothing.",
        "Supreme command: reports to it and requests divisions.",
        "По одному на театр: наступление и преследование, линии обороны, гарнизоны, "
        "участки между своими армиями, высадки и десанты, операции, план театра.",
        "Не меняет свою зону (её рисует ставка), не формирует дивизий, ничего не "
        "производит.",
        "Ставка: докладывает ей и запрашивает дивизии."),
}

# markers: name -> width in bytes (what the harness may write there)
MARKERS = {"HEAD": 110}
MARKERS.update({"ST_" + k.upper(): 100 for k, _, _ in AGENTS})
MARKERS.update({"TH_MODE": 100, "FL_MODE": 100})
MARKERS.update({"TH_%d" % i: 130 for i in range(1, N_THEATRES + 1)})
# The native text boxes scroll independently through a complete byte-bounded part.
# Very long documents retain part navigation rather than silently dropping text.
FEED_ENTRIES, CHAT_ENTRIES = 30, 30
# Native localisation copies stop at 6144 bytes (verified on 1.19.4.1).
# Leave margin; each part still scrolls and longer documents use explicit parts.
MARKERS.update({"FEED": 6000, "CHAT": 6000})
MARKERS.update({"DR_%d" % i: 260 for i in range(1, N_DIRECTIVES + 1)})
MARKERS.update({"DR_TEXT": 6000, "DR_PAGE": 120})
MARKERS.update({"AG_ST": 200, "FD_PAGE": 120})
MARKERS.update({"PL_REVIEW": 160, "PL_IDEA": 900, "PL_TEXT": 6000})
MARKERS.update({"PL_WAR_%d" % i: 520 for i in range(1, N_WARS + 1)})
MARKERS.update({"EV_TITLE": 80, "EV_DESC": 900, "SENT": 200})
MARKERS.update({"SET_NOTE": 300})

W, H = 1040, 620
FEED_WIDTH, FEED_LINES = 552, 240
CHAT_WIDTH, CHAT_LINES = 756, 240


def marker_signature(name):
    """32-bit identity in zero-width native palette escapes (no tabs to normalize)."""
    import zlib
    digits = format(zlib.crc32(name.encode()), '033b')
    palette = 'WYRG BCH!'.replace(' ', '')
    return '\x11H\x11C\x11Y' + ''.join('\x11' + palette[int(digits[i:i+3], 2)]
                                      for i in range(0, 33, 3)) + '\x11!'


def zero_width_pad(text, width):
    remaining = width - len(text.encode('utf-8'))
    assert remaining >= 0
    return text + ' ' * (remaining % 2) + '\x11W' * (remaining // 2)


def marker_source(name, language='english'):
    """Localisation parses § into native byte0x11; sizes apply to the loaded buffer."""
    return marker(name, language).replace('\x11', '§')


def marker(name, language='english', *, width=None):
    ru = language == 'russian'
    defaults = {
        'HEAD': ('Headquarters offline', 'Штаб не подключён'),
        'CHAT': ('No messages or reports yet. Connect the campaign harness to talk to this agent.',
                 'Сообщений и докладов пока нет. Для связи с агентом запусти харнесс кампании.'),
        'FEED': ('No dispatches yet. Reports will appear when the campaign starts.',
                 'Донесений пока нет. Они появятся после запуска кампании.'),
        'AG_ST': ('Awaiting connection to headquarters', 'Ожидается связь со штабом'),
        'DR_1': ('No standing directives.', 'Постоянных директив нет.'),
        'DR_TEXT': ('No standing directives. Send orders in the conversation with the agent.',
                    'Постоянных директив нет. Отправь приказ в переписке с агентом.'),
        'PL_REVIEW': ('No strategic plan received', 'Стратегический план не получен'),
        'PL_IDEA': ('The planner has not submitted a plan yet.', 'Планировщик ещё не представил план.'),
        'PL_TEXT': ('The planner has not submitted a plan yet. Connect headquarters to receive the strategic briefing.',
                    'Планировщик ещё не представил план. Подключи штаб, чтобы получить стратегическую сводку.'),
        'FD_PAGE': ('No dispatches', 'Донесений нет'),
        'TH_MODE': ('Awaiting theatre status', 'Ожидается сводка театров'),
        'FL_MODE': ('Awaiting fleet status', 'Ожидается сводка флотов'),
        'SET_NOTE': ('Settings apply when the campaign harness is connected.',
                     'Настройки применяются при подключённом харнессе кампании.'),
        'EV_TITLE': ('Headquarters request', 'Запрос штаба'),
        'EV_DESC': ('Awaiting headquarters', 'Ожидается сообщение штаба'),
    }
    fallback = defaults.get(name, ('', ''))[int(ru)]
    if name.startswith('ST_'):
        fallback = 'Ожидается доклад' if ru else 'Awaiting first report'
    signature = marker_signature(name)
    return zero_width_pad(fallback, (MARKERS[name] if width is None else width) - len(signature)) + signature


# ------------------------------------------------------------------ gui pieces
def btn(name, x, y, text, sprite="GFX_lc_button", font="hoi_16mbs", tooltip=None):
    return """		buttonType = {
			name = "%s"
			position = { x = %d y = %d }
			quadTextureSprite = "%s"
			buttonText = "%s"
			buttonFont = "%s"
			%s
			clicksound = click_default
			oversound = ui_menu_over
		}""" % (name, x, y, sprite, text, font,
                  'pdx_tooltip = "%s"' % (tooltip or {
                      'LC_SEND': 'LC_TIP_SEND', 'LC_REPORT': 'LC_TIP_REPORT',
                      'LC_SF_OPEN': 'LC_TIP_CONTACT', 'LC_DR_READ': 'LC_TIP_DIRECTIVES',
                      'LC_VIEW_LOG': 'LC_TIP_LOG', 'LC_VIEW_CHAT': 'LC_TIP_CHAT',
                      'LC_PLAN_PREV': 'LC_TIP_PREV', 'LC_PLAN_NEXT': 'LC_TIP_NEXT',
                      'LC_BACK': 'LC_TIP_BACK'}.get(text, 'LC_TIP_CONTROL')))


def checkbox(name, x, y, ticked):
    return """		buttonType = {
			name = "%s"
			position = { x = %d y = %d }
			spriteType = "GFX_lc_check_%s"
			clicksound = click_default
		}""" % (name, x, y, "on" if ticked else "off")


def txt(name, x, y, text, w, h=20, font="hoi_16mbs", align="left"):
    return """		instantTextBoxType = {
			name = "%s"
			position = { x = %d y = %d }
			font = "%s"
			text = "%s"
			maxWidth = %d
			maxHeight = %d
			format = %s
			fixedsize = yes
			alwaysTransparent = yes
		}""" % (name, x, y, font, text, w, h, align)


def icon(name, x, y, sprite):
    return '\n\t\ticonType = { name = "%s" position = { x = %d y = %d } spriteType = "%s" alwaysTransparent = yes }' % (name, x, y, sprite)


def edit(name, x, y, w, h=32):
    """An editbox on a background picture of its own size. A containerWindowType frame
    showed on every tab (24.10: the scripted GUI's _visible does not reach a container),
    so the background is an icon, hidden and shown like any other element."""
    field_sprite("GFX_%s_bg" % name, "%s_bg" % name, w, h)
    return ("""		iconType = {
			name = "%s_bg"
			position = { x = %d y = %d }
			spriteType = "GFX_%s_bg"
			alwaysTransparent = yes
		}""" % (name, x, y, name),
            """		editBoxType = {
			name = "%s"
			position = { x = %d y = %d }
			font = "hoi_16mbs"
			borderSize = { x = 8 y = 0 }
			size = { x = %d y = %d }
			text = ""
			instantTextBoxType = yes
			orientation = "UPPER_LEFT"
			cursor = { x = 0 y = 0 }
		}""" % (name, x, y + 7, w, h))


SPRITES = {}    # GFX name -> texture path inside the mod


def field_sprite(gfx, fname, w, h):
    from PIL import Image, ImageDraw
    rel = "gfx/interface/le_chonk/%s.dds" % fname
    SPRITES[gfx] = (rel, 1)
    path = Path(OUT) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    # The theme's native framed input already has exactly the intended dimensions.
    with Image.open(Path(OUT) / 'gfx/interface/le_chonk/input.dds') as native:
        image = native.convert('RGBA').resize((w,h), Image.Resampling.LANCZOS)
    image.save(path, pixel_format="DXT5")


GAME = os.environ.get("HOI4_DIR", os.path.expanduser(
    "~/.local/share/Steam/steamapps/common/Hearts of Iron IV"))


def checkbox_sprites():
    """GFX_lc_cb_off / GFX_lc_cb_on: the two frames of the game's generic_checkbox.dds,
    one texture each, so a plain button shows the state the harness and the flags say."""
    d = os.path.join(OUT, "gfx", "interface", "le_chonk")
    os.makedirs(d, exist_ok=True)
    src = os.path.join(GAME, "gfx", "interface", "generic_checkbox.dds")
    try:
        from PIL import Image
        im = Image.open(src).convert("RGBA")
        w = im.size[0] // 2
        im.crop((0, 0, w, im.size[1])).save(os.path.join(d, "cb_off.dds"))
        im.crop((w, 0, 2 * w, im.size[1])).save(os.path.join(d, "cb_on.dds"))
    except Exception as e:     # no Pillow or no game here: keep the textures already built
        print("checkbox textures not rebuilt (%s)" % e, file=sys.stderr)
    SPRITES["GFX_lc_cb_off"] = "gfx/interface/le_chonk/cb_off.dds"
    SPRITES["GFX_lc_cb_on"] = "gfx/interface/le_chonk/cb_on.dds"


def write_gfx():
    path = Path(OUT) / "interface/le_chonk.gfx"
    path.parent.mkdir(parents=True, exist_ok=True)
    blocks = []
    for name, (texture, frames) in sorted(SPRITES.items()):
        if name == 'GFX_lc_wait':
            blocks.append('\tframeAnimatedSpriteType = { name = "%s" texturefile = "%s" noOfFrames = %d animation_rate_fps = 12 looping = yes play_on_show = yes pause_on_loop = 0.0 alwaystransparent = yes }\n' % (name, texture, frames))
        else:
            blocks.append('\tspriteType = { name = "%s" texturefile = "%s" noOfFrames = %d }\n' % (name, texture, frames))
    path.write_text("# Generated by mod/build.py.\nspriteTypes = {\n" + "".join(blocks) + "}\n")


ALL_TABS = [t for t, _, _ in TABS] + ["agent"]     # "agent": the agent window


# what a click changed (lc_what), so the harness reads only that part of the panel: a
# page turned is one variable, not thirty flags (24.10: OLDER/NEWER took ~2 s)
WHAT = {"roster": 1, "filter": 2, "window": 3, "feed_page": 4, "send": 5, "answer": 6,
        "settings": 7, "plan_page": 8, "directive_page": 9, "connect": 10}


def what_of(effect_name):
    if effect_name.startswith("lc_directive_"):
        return WHAT["directive_page"]
    if effect_name.startswith("lc_plan_"):
        return WHAT["plan_page"]
    if effect_name in ("lc_send", "lc_report"):
        return WHAT["send"]
    if effect_name.startswith(("lc_s_", "lc_pm_")):
        return WHAT["settings"]
    if effect_name.startswith(("lc_feed_older", "lc_feed_newer")):
        return WHAT["feed_page"]
    if effect_name.startswith(("lc_chat_", "lc_view_", "lc_sf_open", "lc_open_agent_")):
        return WHAT["window"]
    if effect_name.startswith(("lc_show_", "lc_hide_", "lc_important_")):
        return WHAT["filter"]
    return WHAT["roster"]


def tab_cond(tab):
    if tab == "agent_chat":
        return "has_country_flag = lc_tab_agent NOT = { has_country_flag = lc_directives_open }"
    if tab == "agent_directives":
        return "has_country_flag = lc_tab_agent has_country_flag = lc_directives_open"
    if tab == "agents":
        return " ".join("NOT = { has_country_flag = lc_tab_%s }" % t for t in ALL_TABS[1:])
    return "has_country_flag = lc_tab_%s" % tab


def build(output=None):
    global OUT
    OUT = os.path.abspath(output or os.path.join(HERE, "le_chonk"))
    SPRITES.clear()
    import importlib.util
    spec = importlib.util.spec_from_file_location("lc_gui_theme", os.path.join(HERE, "theme.py"))
    theme = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(theme)
    SPRITES.update(theme.build(OUT, W, H))
    elements, effects, triggers = [], [], []
    visible = {}            # element -> tab

    def put(tab, *els):
        for e in els:
            elements.append(e)
            name = e.split('name = "', 1)[1].split('"', 1)[0]
            visible[name] = tab

    def pager(tab, var, x, y):
        """OLDER / NEWER: <var>_page counts pages back from the newest (no scrolling in a
        list of fixed lines, so the harness shows another slice)."""
        put(tab, btn("%s_older" % var, x, y, "LC_OLDER"), btn("%s_newer" % var, x + 122, y,
                                                               "LC_NEWER"))
        effects.append("%s_older_click = { add_to_variable = { %s_page = 1 } add_to_variable = "
                       "{ lc_dirty = 1 } }" % (var, var))
        effects.append("%s_newer_click = { if = { limit = { check_variable = { %s_page > 0 } } "
                       "add_to_variable = { %s_page = -1 } } add_to_variable = { lc_dirty = 1 } }"
                       % (var, var, var))
        triggers.append('%s_newer_click_enabled = { check_variable = { %s_page > 0 } }' % (var, var))

    scroll_guis, scroll_windows = [], []

    # A fixed navigation rail keeps the command area stable between sections.
    for i, (t, _, _) in enumerate(TABS):
        x, y = 18, 110 + i * 48
        base = 'lc_tab_' + t
        others = ' '.join('clr_country_flag = lc_tab_%s' % o for o in ALL_TABS if o != t)
        action = '%s %s' % (others, '' if t == 'agents' else 'set_country_flag = lc_tab_%s' % t)
        for suffix, active in (('', False), ('_active', True)):
            name = base + suffix
            elements.append(btn(name, x, y, 'LC_TAB_' + t.upper(),
                                'GFX_lc_nav_active' if active else 'GFX_lc_nav'))
            effects.append('%s_click = { %s }' % (name, action))
            cond = ("OR = { has_country_flag = lc_tab_staff has_country_flag = lc_tab_agent }"
                    if t == 'staff' else tab_cond(t))
            triggers.append('%s_visible = { %s }' % (name, cond if active else 'NOT = { AND = { %s } }' % cond))
        put(t, txt('lc_section_' + t, 208, 109, 'LC_SECTION_' + t.upper(), 808, 26, 'hoi_20b'))
    elements.extend((txt('lc_offline_head',350,30,'LC_OFFLINE_HEAD',628,40), txt('lc_head', 350, 30, 'LC_HEAD', 628, 40),
                     txt('lc_subtitle', 24, 50, 'LC_SUBTITLE', 290),
                     txt('lc_rail_note', 24, 478, 'LC_RAIL_NOTE', 144, 100)))

    triggers.append('lc_head_visible = { has_country_flag = lc_online NOT = { has_country_flag = lc_view_pending } }')
    triggers.append('lc_offline_head_visible = { NOT = { has_country_flag = lc_online } }')

    def deco(tab, name, x, y, sprite):
        put(tab, icon(name, x, y, sprite))

    def jump(name, key):
        index = next(i for i, item in enumerate(STAFF) if item[0] == key)
        effects.append('%s_click = { %s set_country_flag = lc_tab_agent '
                       'clr_country_flag = lc_directives_open set_variable = { lc_agent_origin = 0 } '
                       'set_variable = { lc_directive_page = 0 } set_variable = { lc_chat_page = 0 } '
                       'set_variable = { lc_agent = %d } add_to_variable = { lc_dirty = 1 } }' %
                       (name, ' '.join('clr_country_flag = lc_tab_%s' % o for o in ALL_TABS), index))

    # Ownership is a labelled action, not an unexplained checkmark.
    for i, (k, _, _) in enumerate(AGENTS):
        y = 151 + i * 49
        deco('agents', 'lc_row_' + k, 208, y, 'GFX_lc_row')
        deco('agents', 'lc_emblem_' + k, 220, y + 9, 'GFX_lc_emblem_' + k)
        if k in CHAT:
            name = 'lc_open_agent_' + k
            put('agents', btn(name, 254, y + 6, 'LC_AG_' + k.upper(), 'GFX_lc_wide'))
            jump(name, k)
        else:
            put('agents', txt('lc_' + k + '_label', 266, y + 14, 'LC_AG_' + k.upper(), 218))
        put('agents', txt('lc_' + k + '_status', 494, y + 7, 'LC_ST_' + k.upper(), 398, 38))
        for on in (True, False):
            name = 'lc_%s_%s' % (k, 'on' if on else 'off')
            elements.append(btn(name, 918, y + 9, 'LC_CONTROL_AGENT' if on else 'LC_CONTROL_YOU',
                                'GFX_lc_switch_active' if on else 'GFX_lc_switch',
                                tooltip='LC_HANDOVER_YOU' if on else 'LC_HANDOVER_AGENT'))
            condition = ('NOT = { has_country_flag = lc_off_%s }' if on else
                         'has_country_flag = lc_off_%s') % k
            triggers.append('%s_visible = { %s %s }' % (name, tab_cond('agents'), condition))
            effects.append('%s_click = { %s_country_flag = lc_off_%s add_to_variable = { lc_dirty = 1 } }' %
                           (name, 'set' if on else 'clr', k))
    put('agents', txt('lc_askwar_label', 246, 564, 'LC_AG_ASKWAR', 720, 36))
    for name, on, cond, fx in (
            ('lc_askwar_no', False, 'NOT = { has_country_flag = lc_ask_war }', 'set_country_flag = lc_ask_war'),
            ('lc_askwar_yes', True, 'has_country_flag = lc_ask_war', 'clr_country_flag = lc_ask_war')):
        elements.append(checkbox(name, 214, 561, on))
        effects.append('%s_click = { %s add_to_variable = { lc_dirty = 1 } }' % (name, fx))
        triggers.append('%s_visible = { %s %s }' % (name, tab_cond('agents'), cond))

    # Command tree and one readable role card; selected row has a brass keyline.
    deco('staff', 'lc_staff_card', 468, 151, 'GFX_lc_card')
    for i, (k, depth, _, _) in enumerate(STAFF):
        y = 151 + i * 43
        cond = 'check_variable = { lc_card = %d }' % i if i else 'NOT = { check_variable = { lc_card > 0 } }'
        for suffix, active in (('', False), ('_active', True)):
            name = 'lc_sf_' + k + suffix
            elements.append(btn(name, 208+depth*12, y, 'LC_SF_' + k.upper(),
                                'GFX_lc_staff_active' if active else 'GFX_lc_staff'))
            effects.append('%s_click = { set_variable = { lc_card = %d } }' % (name, i))
            triggers.append('%s_visible = { %s %s }' %
                            (name, tab_cond('staff'), cond if active else 'NOT = { AND = { %s } }' % cond))
        elements.append(icon('lc_card_emblem_' + k, 486, 168, 'GFX_lc_emblem_' + k))
        triggers.append('lc_card_emblem_%s_visible = { %s %s }' % (k, tab_cond('staff'), cond))
        elements.append(txt('lc_card_' + k, 486, 213, 'LC_CARD_' + k.upper(), 508, 304))
        triggers.append('lc_card_%s_visible = { %s %s }' % (k, tab_cond('staff'), cond))
        if k != 'marshals':
            for who, owncond in (('agent', 'NOT = { has_country_flag = lc_off_%s }' % k),
                                 ('you', 'has_country_flag = lc_off_%s' % k)):
                name = 'lc_card_who_' + k + '_' + who
                elements.append(txt(name, 530, 173, 'LC_SF_' + who.upper(), 450))
                triggers.append('%s_visible = { %s %s %s }' % (name, tab_cond('staff'), cond, owncond))
    put('staff', txt('lc_sf_hint', 208, 553, 'LC_SF_HINT', 224, 42))
    elements.append(btn('lc_sf_open', 486, 518, 'LC_SF_OPEN', 'GFX_lc_wide'))
    effects.append('lc_sf_open_click = { %s set_country_flag = lc_tab_agent '
                   'clr_country_flag = lc_directives_open set_variable = { lc_agent_origin = 1 } '
                   'set_variable = { lc_directive_page = 0 } set_variable = { lc_chat_page = 0 } '
                   'set_variable = { lc_agent = lc_card } add_to_variable = { lc_dirty = 1 } }' %
                   ' '.join('clr_country_flag = lc_tab_%s' % o for o in ALL_TABS if o != 'agent'))
    no_chat = ' '.join('NOT = { check_variable = { lc_card = %d } }' % i
                       for i, (k, _, _, _) in enumerate(STAFF) if k not in CHAT)
    triggers.append('lc_sf_open_visible = { %s %s }' % (tab_cond('staff'), no_chat))

    # Command ownership modes above compact theatre cards.
    for i, (k, mk) in enumerate((('theatres', 'TH_MODE'), ('fleets', 'FL_MODE'))):
        y = 151 + i * 49
        deco('theatres', 'lc_mode_row_' + k, 208, y, 'GFX_lc_row')
        put('theatres', txt('lc_' + k + '_label', 224, y+13, 'LC_AG_' + k.upper(), 125),
            txt('lc_' + k + '_status', 525, y+7, 'LC_' + mk, 470, 38))
        for m in ('all', 'tagged', 'none'):
            elements.append(btn('lc_%s_%s' % (k, m), 356, y+6, 'LC_MODE_' + m.upper(), 'GFX_lc_mode'))
        effects.extend((
            'lc_%s_all_click = { set_country_flag = lc_%s_tagged add_to_variable = { lc_dirty = 1 } }' % (k,k),
            'lc_%s_tagged_click = { clr_country_flag = lc_%s_tagged set_country_flag = lc_%s_none add_to_variable = { lc_dirty = 1 } }' % (k,k,k),
            'lc_%s_none_click = { clr_country_flag = lc_%s_none add_to_variable = { lc_dirty = 1 } }' % (k,k)))
        for m, condition in (('all', 'NOT = { has_country_flag = lc_%s_tagged } NOT = { has_country_flag = lc_%s_none }' % (k,k)),
                              ('tagged', 'has_country_flag = lc_%s_tagged' % k),
                              ('none', 'has_country_flag = lc_%s_none' % k)):
            triggers.append('lc_%s_%s_visible = { %s %s }' % (k,m,tab_cond('theatres'),condition))
    put('theatres', txt('lc_th_title', 208, 258, 'LC_TH_TITLE', 808, 24, 'hoi_18mbs'))
    for i in range(1, N_THEATRES + 1):
        y = 292 + (i-1)*35
        put('theatres', txt('lc_th_%d' % i, 224, y, 'LC_TH_%d' % i, 776, 34))
    put('theatres', txt('lc_th_hint', 208, 576, 'LC_TH_HINT', 808, 28))

    # Reports use a fixed, transparent text box: no enormous hover copy of the feed.
    deco('reports', 'lc_feed_bg', 208, 151, 'GFX_lc_feed')
    deco('reports', 'lc_filter_bg', 828, 151, 'GFX_lc_filter')
    put('reports', txt('lc_feed_text', 224, 169, 'LC_FEED', 552, 372),
        txt('lc_filter_title', 844, 168, 'LC_FILTER_TITLE', 156, 36))
    y = 210
    for k, _, _, _ in STAFF:
        for name, ticked, condition, fx in (
                ('lc_show_'+k, True, 'NOT = { has_country_flag = lc_mute_%s }' % k, 'set_country_flag = lc_mute_%s' % k),
                ('lc_hide_'+k, False, 'has_country_flag = lc_mute_%s' % k, 'clr_country_flag = lc_mute_%s' % k)):
            elements.append(checkbox(name, 844, y, ticked))
            effects.append('%s_click = { %s add_to_variable = { lc_dirty = 1 } }' % (name,fx))
            triggers.append('%s_visible = { %s %s }' % (name,tab_cond('reports'),condition))
        put('reports', txt('lc_filter_label_'+k, 876, y+3, 'LC_FILTER_'+k.upper(), 124))
        y += 32
    for name, on, condition, fx in (
            ('lc_important_on', True, 'has_country_flag = lc_feed_important', 'clr_country_flag = lc_feed_important'),
            ('lc_important_off', False, 'NOT = { has_country_flag = lc_feed_important }', 'set_country_flag = lc_feed_important')):
        elements.append(checkbox(name, 844, 518, on))
        effects.append('%s_click = { %s add_to_variable = { lc_dirty = 1 } }' % (name,fx))
        triggers.append('%s_visible = { %s %s }' % (name,tab_cond('reports'),condition))
    put('reports', txt('lc_important_label', 876, 521, 'LC_IMPORTANT', 124, 36),
        txt('lc_fd_page', 208, 580, 'LC_FD_PAGE', 364))
    pager('reports', 'lc_feed', 580, 574)

    def agent_busy(value=None):
        parts = []
        for i, (key, *_rest) in enumerate(STAFF):
            if key in CHAT:
                check = 'check_variable = { lc_busy_%s %s }' % (key, '> 0' if value is None else '= %d' % value)
                parts.append('AND = { check_variable = { lc_agent = %d } %s }' % (i, check))
        return 'OR = { %s }' % ' '.join(parts)
    busy = agent_busy()
    queued = 'OR = { has_country_flag = lc_send has_country_flag = lc_report }'
    selected_queue = 'AND = { %s check_variable = { lc_agent = lc_request_agent } }' % queued
    idle = 'OR = { NOT = { has_country_flag = lc_online } AND = { NOT = { %s } NOT = { %s } } }' % (busy, selected_queue)
    available = ('has_country_flag = lc_online NOT = { has_country_flag = lc_view_pending } '
                 'NOT = { has_country_flag = lc_text_error } NOT = { %s } NOT = { %s }' % (busy, queued))
    put('agent_chat', txt('lc_offline_warning',208,554,'LC_OFFLINE_WARNING',808))
    triggers.append('lc_offline_warning_visible = { %s NOT = { has_country_flag = lc_online } }' % tab_cond('agent_chat'))
    # put() normally adds a tab trigger: explicit conditions take precedence below.
    visible.pop('lc_offline_warning')
    elements.append(txt('lc_offline_agent',208,143,'LC_OFFLINE_AGENT',808))
    triggers.append('lc_offline_agent_visible = { %s NOT = { has_country_flag = lc_online } }' % tab_cond('agent'))
    elements.extend((icon('lc_request_spinner',208,282,'GFX_lc_wait'),
                     icon('lc_refresh_spinner',28,548,'GFX_lc_wait'),
                     txt('lc_refresh_label',52,550,'LC_VIEW_UPDATING',118,46),
                     txt('lc_refresh_error',28,548,'LC_VIEW_ERROR',146,60)))
    triggers.extend((
        'lc_request_spinner_visible = { %s has_country_flag = lc_online OR = { %s %s } }' % (tab_cond('agent_chat'),busy,selected_queue),
        'lc_refresh_spinner_visible = { has_country_flag = lc_online has_country_flag = lc_view_pending }',
        'lc_refresh_label_visible = { has_country_flag = lc_online has_country_flag = lc_view_pending }',
        'lc_refresh_error_visible = { has_country_flag = lc_online has_country_flag = lc_text_error }',
        'lc_send_click_enabled = { %s }' % available,
        'lc_report_click_enabled = { %s }' % available))

    # Conversation: directives stay visible above a full-width reading area.
    for i, (k, _, _, _) in enumerate(STAFF):
        if k in CHAT:
            elements.append(txt('lc_ag_name_'+k, 208, 109, 'LC_SF_'+k.upper(), 310, 26, 'hoi_20b'))
            triggers.append('lc_ag_name_%s_visible = { %s check_variable = { lc_agent = %d } }' % (k,tab_cond('agent'),i))
    put('agent', btn('lc_ag_back', 904, 106, 'LC_BACK'))
    effects.append('lc_ag_back_click = { clr_country_flag = lc_tab_agent clr_country_flag = lc_directives_open '
                   'if = { limit = { check_variable = { lc_agent_origin = 1 } } set_country_flag = lc_tab_staff } }')
    deco('agent_chat', 'lc_directive_bg', 208, 172, 'GFX_lc_directive')
    put('agent', txt('lc_ag_st', 208, 143, 'LC_AG_ST', 808))
    put('agent_chat', txt('lc_sent', 208, 554, 'LC_SENT', 808))
    put('agent_chat', txt('lc_dr_title', 224, 181, 'LC_DR_TITLE', 510),
        btn('lc_directive_open', 776, 175, 'LC_DR_READ', 'GFX_lc_wide'))
    effects.append('lc_directive_open_click = { set_country_flag = lc_directives_open '
                   'add_to_variable = { lc_dirty = 1 } }')
    # The background must precede its texts in the drawing order.
    for i in range(1, N_DIRECTIVES+1):
        put('agent_chat', txt('lc_dr_%d' % i, 224, 212+(i-1)*20, 'LC_DR_%d' % i, 776, 20))
    for name, text, condition, fx in (
            ('lc_view_chat','LC_VIEW_CHAT','has_country_flag = lc_agent_log','clr_country_flag = lc_agent_log'),
            ('lc_view_log','LC_VIEW_LOG','NOT = { has_country_flag = lc_agent_log }','set_country_flag = lc_agent_log')):
        elements.append(btn(name, 792, 278, text, 'GFX_lc_wide'))
        effects.append('%s_click = { %s add_to_variable = { lc_dirty = 1 } }' % (name,fx))
        triggers.append('%s_visible = { %s %s }' % (name,tab_cond('agent_chat'),condition))
    for name, text, condition, loading in (
            ('lc_view_title_chat','LC_VIEW_TITLE_CHAT','NOT = { has_country_flag = lc_agent_log } ' + idle, False),
            ('lc_view_title_log','LC_VIEW_TITLE_LOG','has_country_flag = lc_agent_log ' + idle, False),
            ('lc_view_queued','LC_VIEW_QUEUED','has_country_flag = lc_online ' + selected_queue, True),
            ('lc_view_sending','LC_VIEW_SENDING','has_country_flag = lc_online NOT = { %s } ' % selected_queue + agent_busy(1), True),
            ('lc_view_reporting','LC_VIEW_REPORTING','has_country_flag = lc_online NOT = { %s } ' % selected_queue + agent_busy(2), True)):
        elements.append(txt(name,234 if loading else 208,286,text,294 if loading else 318))
        triggers.append('%s_visible = { %s %s }' % (name,tab_cond('agent_chat'),condition))
    deco('agent_chat','lc_chat_bg',208,320,'GFX_lc_chat')
    put('agent_chat', txt('lc_chat_text', 224, 337, 'LC_CHAT', 756, 204))
    put('agent_chat',*edit('lc_input',208,580,514),btn('lc_send',736,580,'LC_SEND'),btn('lc_report',862,580,'LC_REPORT','GFX_lc_report'))
    pager('agent_chat','lc_chat',538,278)
    deco('agent_directives', 'lc_directives_reader_bg', 208, 172, 'GFX_lc_reader')
    put('agent_directives', txt('lc_directives_title',224,188,'LC_DR_TITLE',510),
        btn('lc_directive_close',776,181,'LC_DR_CLOSE','GFX_lc_wide'),
        txt('lc_directives_text', 224, 232, 'LC_DR_TEXT', 756, 320),
        txt('lc_directives_page',224,582,'LC_DR_PAGE',290),
        btn('lc_directive_prev',772,574,'LC_PLAN_PREV'),
        btn('lc_directive_next',894,574,'LC_PLAN_NEXT'))
    effects.extend((
        'lc_directive_close_click = { clr_country_flag = lc_directives_open add_to_variable = { lc_dirty = 1 } }',
        'lc_directive_prev_click = { if = { limit = { check_variable = { lc_directive_page > 0 } } add_to_variable = { lc_directive_page = -1 } } add_to_variable = { lc_dirty = 1 } }',
        'lc_directive_next_click = { add_to_variable = { lc_directive_page = 1 } add_to_variable = { lc_dirty = 1 } }'))
    triggers.append('lc_directive_prev_click_enabled = { check_variable = { lc_directive_page > 0 } }')
    effects.append('lc_send_click = { set_variable = { lc_request_agent = lc_agent } set_country_flag = lc_send clr_country_flag = lc_agent_log set_variable = { lc_chat_page = 0 } add_to_variable = { lc_dirty = 1 } }')
    effects.append('lc_report_click = { set_variable = { lc_request_agent = lc_agent } set_country_flag = lc_report clr_country_flag = lc_agent_log set_variable = { lc_chat_page = 0 } add_to_variable = { lc_dirty = 1 } }')

    deco('plan','lc_plan_bg',208,185,'GFX_lc_reader')
    put('plan', btn('lc_plan_prev',772,106,'LC_PLAN_PREV'),
        btn('lc_plan_next',894,106,'LC_PLAN_NEXT'))
    effects.append('lc_plan_prev_click = { if = { limit = { check_variable = { lc_plan_page > 0 } } add_to_variable = { lc_plan_page = -1 } } add_to_variable = { lc_dirty = 1 } }')
    effects.append('lc_plan_next_click = { add_to_variable = { lc_plan_page = 1 } add_to_variable = { lc_dirty = 1 } }')
    triggers.append('lc_plan_prev_click_enabled = { check_variable = { lc_plan_page > 0 } }')
    put('plan',txt('lc_pl_review',208,151,'LC_PL_REVIEW',808),
        txt('lc_pl_text', 224, 201, 'LC_PL_TEXT', 756, 356),
        txt('lc_scroll_hint',224,582,'LC_SCROLL_HINT',776))

    # Settings: a readable label column, bounded steppers and a separate pause policy.
    deco('settings','lc_pace_bg',208,151,'GFX_lc_settings')
    deco('settings','lc_pause_bg',620,151,'GFX_lc_settings')
    put('settings',txt('lc_set_title',224,168,'LC_SET_TITLE',360),
        txt('lc_pm_title',636,168,'LC_PM_TITLE',360))
    for i,(k,lo,hi,_,_) in enumerate(ROSTER.SETTINGS):
        y=205+i*43
        v,s='lc_s_'+k,STEP.get(k,1)
        put('settings',txt('lc_s_%s_label'%k,224,y+3,'LC_S_'+k.upper(),238,36),
            btn('lc_s_%s_dec'%k,474,y,'LC_MINUS','GFX_lc_step'),
            txt('lc_s_%s_value'%k,508,y+4,'LC_SV_'+k.upper(),44,align='centre'),
            btn('lc_s_%s_inc'%k,558,y,'LC_PLUS','GFX_lc_step'))
        triggers.append('lc_s_%s_dec_click_enabled = { has_country_flag = lc_online check_variable = { %s > %d } }' % (k,v,lo))
        triggers.append('lc_s_%s_inc_click_enabled = { has_country_flag = lc_online check_variable = { %s < %d } }' % (k,v,hi))
        effects.append('lc_s_%s_dec_click = { if = { limit = { check_variable = { %s > %d } } add_to_variable = { %s = -%d } } else = { set_variable = { %s = %d } } add_to_variable = { lc_dirty = 1 } }'%(k,v,lo+s-1,v,s,v,lo))
        effects.append('lc_s_%s_inc_click = { if = { limit = { check_variable = { %s < %d } } add_to_variable = { %s = %d } } else = { set_variable = { %s = %d } } add_to_variable = { lc_dirty = 1 } }'%(k,v,hi-s+1,v,s,v,hi))
    for j,k in enumerate(ROSTER.PAUSE_AGENTS):
        y=205+j*39
        put('settings',txt('lc_pm_%s_label'%k,636,y+7,'LC_PM_'+k.upper(),188))
        for i,m in enumerate(ROSTER.PAUSE_MODES):
            name='lc_pm_%s_%s'%(k,m)
            elements.append(btn(name,844,y,'LC_PM_MODE_'+m.upper(),'GFX_lc_mode'))
            effects.append('%s_click = { set_variable = { lc_pm_%s = %d } add_to_variable = { lc_dirty = 1 } }'%(name,k,(i+1)%len(ROSTER.PAUSE_MODES)))
            condition='check_variable = { lc_pm_%s = %d }'%(k,i) if i else 'NOT = { check_variable = { lc_pm_%s > 0 } }'%k
            triggers.append('%s_visible = { %s %s }'%(name,tab_cond('settings'),condition))
    put('settings',btn('lc_api_setup',844,548,'LC_API_SETUP','GFX_lc_mode',tooltip='LC_TIP_API_SETUP'),
        txt('lc_api_hint',208,550,'LC_API_HINT',618,32),
        txt('lc_set_hint',208,582,'LC_SET_HINT',808,32))

    effects.append('lc_api_setup_click = { set_country_flag = lc_setup_requested add_to_variable = { lc_dirty = 1 } }')
    triggers.append('lc_api_setup_click_enabled = { has_country_flag = lc_online }')

    for name in [*(('lc_%s_%s' % (key, state)) for key, *_ in AGENTS for state in ('on','off')),
                 'lc_askwar_yes','lc_askwar_no',
                 *(('lc_%s_%s' % (key, mode)) for key in ('theatres','fleets') for mode in ('all','tagged','none')),
                 *(('lc_pm_%s_%s' % (key, mode)) for key in ROSTER.PAUSE_AGENTS for mode in ROSTER.PAUSE_MODES)]:
        triggers.append('%s_click_enabled = { has_country_flag = lc_online }' % name)
    part_buttons = {'lc_plan_prev': 'plan', 'lc_plan_next': 'plan',
                    'lc_directive_prev': 'directive', 'lc_directive_next': 'directive',
                    'lc_feed_older': 'feed', 'lc_feed_newer': 'feed',
                    'lc_chat_older': 'chat', 'lc_chat_newer': 'chat'}
    for name, tab in visible.items():
        extra = (' check_variable = { lc_%s_last > 0 }' % part_buttons[name]) if name in part_buttons else ''
        if name in ('lc_sent','lc_ag_st','lc_chat_text','lc_feed_text','lc_pl_text','lc_directives_text') or name.startswith('lc_dr_'):
            extra += ' NOT = { AND = { has_country_flag = lc_online has_country_flag = lc_view_pending } }'
        if name in ('lc_sent','lc_ag_st'):
            extra += ' has_country_flag = lc_online'
        triggers.append("%s_visible = { %s%s }" % (name, tab_cond(tab), extra))
    for control, kind in (('lc_plan_next','plan'),('lc_directive_next','directive'),
                          ('lc_feed_older','feed'),('lc_chat_older','chat')):
        triggers.append('%s_click_enabled = { check_variable = { lc_%s_page < lc_%s_last } }' % (control,kind,kind))
    bump = "add_to_variable = { lc_dirty = 1 }"
    routed = []
    for action in effects:
        what = what_of(action.split('_click',1)[0])
        wait = ('if = { limit = { has_country_flag = lc_online } set_country_flag = lc_view_pending } '
                if what in (WHAT['window'],WHAT['filter'],WHAT['feed_page'],WHAT['plan_page'],WHAT['directive_page']) else '')
        routed.append(action.replace(bump, 'set_variable = { lc_what = %d } %s%s' % (what,wait,bump)))
    effects = routed


    gui = """# Generated by mod/build.py: edit the tables there, not this file.
scripted_gui = {
	lc_topbar = {
		context_type = player_context
		window_name = "lc_topbar_window"
		parent_window_token = top_bar
		visible = { always = yes }
		effects = {
			lc_open_click = {
				if = { limit = { has_country_flag = lc_panel_open } clr_country_flag = lc_panel_open }
				else = { set_country_flag = lc_panel_open lc_ui_disconnect = yes
                    set_variable = { lc_what = 10 } add_to_variable = { lc_dirty = 1 } }
			}
		}
		ai_enabled = { always = no }
	}
	lc_panel = {
		context_type = player_context
		window_name = "lc_panel_window"
		visible = { has_country_flag = lc_panel_open }
		effects = {
			lc_close_click = { clr_country_flag = lc_panel_open }
%s
		}
		triggers = {
%s
		}
		ai_enabled = { always = no }
	}
%s
}
""" % ("\n".join("\t\t\t" + e for e in effects), "\n".join("\t\t\t" + t for t in triggers),
       "\n".join(scroll_guis))

    ui = """# Generated by mod/build.py.
guiTypes = {
	containerWindowType = {
		name = "lc_topbar_window"
		position = { x = -382 y = 42 }
		orientation = UPPER_RIGHT
		size = { width = 46 height = 46 }
%s
	}

	containerWindowType = {
		name = "lc_panel_window"
		position = { x = 80 y = 70 }
		size = { width = %d height = %d }
		moveable = yes
		background = {
			name = "lc_bg"
			quadTextureSprite = "GFX_lc_window"
		}
%s
		buttonType = {
			name = "lc_close"
			position = { x = %d y = 14 }
			quadTextureSprite = "GFX_closebutton"
			clicksound = click_close
		}
%s
	}
%s
}
""" % (btn("lc_open", 0, 0, "", "GFX_lc_open", tooltip="LC_OPEN"), W, H, txt("lc_title", 24, 21, "LC_TITLE", 290, 32, "hoi_30header"),
       W - 40, "\n".join(elements), "\n".join(scroll_windows))

    write_gfx()
    os.makedirs(os.path.join(OUT, "common", "scripted_guis"), exist_ok=True)
    os.makedirs(os.path.join(OUT, "common", "scripted_effects"), exist_ok=True)
    os.makedirs(os.path.join(OUT, "interface"), exist_ok=True)
    os.makedirs(os.path.join(OUT, "events"), exist_ok=True)
    Path(OUT, "common", "scripted_guis", "le_chonk_scripted_gui.txt").write_text(gui)
    Path(OUT, "interface", "le_chonk.gui").write_text(
        "\n".join(line.rstrip() for line in ui.splitlines()) + "\n")

    # scripted effects the harness runs through the console
    fx = ["# Generated by mod/build.py. Run by the harness: effect <name>."]
    for k, _, _ in AGENTS:
        fx.append("lc_set_off_%s = { set_country_flag = lc_off_%s }" % (k, k))
        fx.append("lc_set_on_%s = { clr_country_flag = lc_off_%s }" % (k, k))
    for k in ("theatres", "fleets"):
        fx.append("lc_set_%s_all = { clr_country_flag = lc_%s_tagged clr_country_flag = lc_%s_none }"
                  % (k, k, k))
        fx.append("lc_set_%s_tagged = { set_country_flag = lc_%s_tagged clr_country_flag = "
                  "lc_%s_none }" % (k, k, k))
        fx.append("lc_set_%s_none = { clr_country_flag = lc_%s_tagged set_country_flag = "
                  "lc_%s_none }" % (k, k, k))
    fx.append("lc_set_ask_war_on = { set_country_flag = lc_ask_war }")
    fx.append("lc_set_ask_war_off = { clr_country_flag = lc_ask_war }")
    clear_busy = ' '.join('set_variable = { lc_busy_%s = 0 }' % key for key in CHAT)
    clear_pending = ('clr_country_flag = lc_send clr_country_flag = lc_report '
                     'clr_country_flag = lc_view_pending clr_country_flag = lc_text_error ' + clear_busy)
    fx.extend((
        'lc_ui_connect = { %s set_country_flag = lc_online set_country_flag = lc_view_pending }' % clear_pending,
        'lc_ui_disconnect = { clr_country_flag = lc_online clr_country_flag = lc_setup_requested %s }' % clear_pending,
        'lc_setup_consumed = { clr_country_flag = lc_setup_requested }',
        'lc_ui_keepalive = { set_country_flag = lc_online }',
        'lc_ui_paint_ok = { if = { limit = { check_variable = { lc_dirty = lc_painted_dirty } } clr_country_flag = lc_view_pending clr_country_flag = lc_text_error } }',
        'lc_ui_paint_error = { if = { limit = { check_variable = { lc_dirty = lc_painted_dirty } } clr_country_flag = lc_view_pending set_country_flag = lc_text_error } }'))
    fx.append("lc_ping = { add_to_variable = { lc_pong = 1 } }")
    for f in ("send", "report", "answer_yes", "answer_no"):
        fx.append("lc_clear_%s = { clr_country_flag = lc_%s }" % (f, f))
    fx.append("lc_ask = { country_event = { id = lechonk.1 } }")
    Path(OUT, "common", "scripted_effects", "le_chonk_effects.txt").write_text(
        "\n".join(fx) + "\n")

    # the question to the player: approve or reject (what is asked is a marker)
    ev = """# Generated by mod/build.py.
add_namespace = lechonk

country_event = {
	id = lechonk.1
	title = LC_EV_TITLE
	desc = LC_EV_DESC
	picture = GFX_report_event_generic_read_write
	is_triggered_only = yes
	option = {
		name = LC_EV_YES
		set_country_flag = lc_answer_yes
		set_variable = { lc_what = 6 }
		add_to_variable = { lc_dirty = 1 }
	}
	option = {
		name = LC_EV_NO
		set_country_flag = lc_answer_no
		set_variable = { lc_what = 6 }
		add_to_variable = { lc_dirty = 1 }
	}
}
"""
    Path(OUT, "events", "le_chonk_events.txt").write_text(ev)

    # localisation
    en = {"LC_OPEN": "Le Chonk", "LC_TITLE": "LE CHONK",
          "LC_SF_AGENT": "§Gagent§!", "LC_SF_YOU": "§Yyou§!", "LC_SF_SEE_THEATRES": "theatres",
          "LC_SF_HINT": "Click an agent to see what it decides. Green: the agent plays it. "
                        "Yellow: you do.",
          "LC_MODE_ALL": "ALL TO AGENTS", "LC_MODE_TAGGED": "ONLY AI:", "LC_MODE_NONE": "ALL YOURS",
          "LC_AG_THEATRES": "Theatres", "LC_AG_FLEETS": "Fleets",
          "LC_HINT": "Ticked: the agent plays this branch. Unticked: you do, and what the "
                     "agents need from it comes to you as a report.",
          "LC_TH_TITLE": "Theatres under our field marshals",
          "LC_TH_HINT": "In ONLY AI: mode a theatre or fleet whose name begins with AI: is the "
                        "agents'. Rename it to hand it over or to take it back.",
          "LC_SEND": "SEND",
          "LC_EV_YES": "Approve", "LC_EV_NO": "Reject",
          "LC_AG_ASKWAR": "Ask me before declaring war",
          "LC_SET_TITLE": "§HThe pace of the game§!", "LC_PM_TITLE": "§HTheir turns stop the game§!",
          "LC_PM_MODE_ALWAYS": "ALWAYS", "LC_PM_MODE_BATTLE": "PEACE / LAND WAR",
          "LC_PM_MODE_ALARMS": "ON ALARM",
          "LC_SET_HINT": "ALWAYS: every turn of the agent stops the game. PEACE, BATTLE: in peace "
                         "and while we fight on land, not in a war with no battle. ON ALARM: the "
                         "game runs while it thinks and stops only for a new war, a lost state "
                         "or a hole in the front. Speeds are the game's 1-5."}
    ru = {"LC_OPEN": "Le Chonk", "LC_TITLE": "LE CHONK",
          "LC_SF_AGENT": "§Gагент§!", "LC_SF_YOU": "§Yвы§!", "LC_SF_SEE_THEATRES": "театры",
          "LC_SF_HINT": "Нажмите на агента, чтобы увидеть, что он решает. Зелёный: играет "
                        "агент. Жёлтый: вы.",
          "LC_MODE_ALL": "ВСЕ АГЕНТАМ", "LC_MODE_TAGGED": "ТОЛЬКО AI:", "LC_MODE_NONE": "ВСЕ ВАШИ",
          "LC_AG_THEATRES": "Театры", "LC_AG_FLEETS": "Флоты",
          "LC_HINT": "С галочкой: этой ветвью управляет агент. Без галочки: вы сами, а что "
                     "агентам от неё нужно, придёт вам донесением.",
          "LC_TH_TITLE": "Театры под нашими маршалами",
          "LC_TH_HINT": "В режиме ТОЛЬКО AI: театр или флот, чьё название начинается с AI:, "
                        "принадлежит агентам. Переименуйте, чтобы отдать или забрать.",
          "LC_SEND": "ОТПРАВИТЬ",
          "LC_EV_YES": "Одобрить", "LC_EV_NO": "Отклонить",
          "LC_AG_ASKWAR": "Спрашивать меня перед объявлением войны",
          "LC_SET_TITLE": "§HТемп игры§!", "LC_PM_TITLE": "§HИх ходы ставят игру на паузу§!",
          "LC_PM_MODE_ALWAYS": "ВСЕГДА", "LC_PM_MODE_BATTLE": "МИР И БОИ",
          "LC_PM_MODE_ALARMS": "ПО ТРЕВОГЕ",
          "LC_SET_HINT": "ВСЕГДА: каждый ход агента ставит игру на паузу. МИР И БОИ: в мире и "
                         "пока мы воюем на суше, но не в войне без боёв. ПО ТРЕВОГЕ: игра идёт, "
                         "пока он думает, и встаёт только на новую войну, потерю области или "
                         "дыру во фронте. Скорости как в игре, 1-5."}
    en.update({"LC_SUBTITLE": "COMMAND DESK", "LC_RAIL_NOTE": "YOUR COUNTRY.\\nYOUR COMMAND.",
               "LC_HANDOVER_YOU": "Take control of this branch. The agent will stop issuing orders here.",
               "LC_HANDOVER_AGENT": "Delegate this branch to its agent. Your standing directives still apply.",
               "LC_DR_READ": "READ ALL DIRECTIVES", "LC_DR_CLOSE": "BACK TO CONVERSATION",
               "LC_PLAN_PREV": "PREVIOUS", "LC_PLAN_NEXT": "NEXT",
               "LC_CONTROL_AGENT": "AGENT", "LC_CONTROL_YOU": "YOU", "LC_MINUS": "-", "LC_PLUS": "+",
               "LC_FILTER_TITLE": "SOURCES", "LC_SET_TITLE": "PACE & REVIEWS", "LC_PM_TITLE": "PAUSE POLICY",
               "LC_SET_HINT": "ALWAYS: pause each turn. PEACE / LAND WAR: pause in peace and on land. ON ALARM: pause only for an emergency.",
               "LC_SF_HINT": "Select a role. Contact its agent for orders, questions and reports."})
    ru.update({"LC_SUBTITLE": "КОМАНДНЫЙ ПУНКТ", "LC_RAIL_NOTE": "ТВОЯ СТРАНА.\\nТВОИ РЕШЕНИЯ.",
               "LC_HANDOVER_YOU": "Взять управление этой ветвью на себя. Агент перестанет отдавать здесь приказы.",
               "LC_HANDOVER_AGENT": "Передать ветвь агенту. Твои постоянные директивы остаются в силе.",
               "LC_DR_READ": "ВСЕ ДИРЕКТИВЫ", "LC_DR_CLOSE": "К ПЕРЕПИСКЕ",
               "LC_PLAN_PREV": "НАЗАД", "LC_PLAN_NEXT": "ДАЛЕЕ",
               "LC_CONTROL_AGENT": "АГЕНТ", "LC_CONTROL_YOU": "ТЫ", "LC_MINUS": "-", "LC_PLUS": "+",
               "LC_SET_TITLE": "ТЕМП И ОБЗОРЫ", "LC_PM_TITLE": "ПРАВИЛА ПАУЗЫ",
               "LC_SET_HINT": "ВСЕГДА: пауза каждый ход. МИР И БОИ: в мире и в сухопутной войне. ТРЕВОГА: только при угрозе.",
               "LC_SF_HINT": "Выбери роль. Свяжись с агентом для приказов, вопросов и докладов."})
    sections = {"agents": ("Control & delegation", "Управление"), "staff": ("Chain of command", "Штаб"),
                "theatres": ("Theatres & fleets", "Театры и флоты"), "reports": ("Dispatches", "Донесения"),
                "plan": ("Strategic plan", "Стратегический план"), "settings": ("Campaign settings", "Настройки кампании")}
    for key, (eng, rus) in sections.items():
        en["LC_SECTION_" + key.upper()], ru["LC_SECTION_" + key.upper()] = eng, rus
    for k, e, r in AGENTS:
        en["LC_AG_" + k.upper()], ru["LC_AG_" + k.upper()] = e, r
    for k, e, r in TABS:
        en["LC_TAB_" + k.upper()], ru["LC_TAB_" + k.upper()] = e, r
    for k, _, e, r in STAFF:
        c = BADGE[k]
        en["LC_FILTER_" + k.upper()] = "§%s%s§!" % (c, e.split(" (")[0])
        ru["LC_FILTER_" + k.upper()] = "§%s%s§!" % (c, r.split(" (")[0])
    en.update({"LC_FILTER_TITLE": "Show reports from", "LC_IMPORTANT": "Important only",
               "LC_SF_OPEN": "CONTACT AGENT", "LC_BACK": "BACK", "LC_VIEW_CHAT": "CONVERSATION",
               "LC_VIEW_LOG": "TURN REPORTS", "LC_REPORT": "ASK FOR REPORT",
               "LC_END": " ",
               "LC_DR_TITLE": "§HYour standing directives§!", "LC_OLDER": "OLDER",
               "LC_VIEW_TITLE_CHAT": "CONVERSATION",
               "LC_VIEW_TITLE_LOG": "TURN REPORTS",
               "LC_VIEW_SENDING": "§YWaiting for the agent’s reply...§!",
               "LC_VIEW_REPORTING": "§YPreparing a report...§!",
               "LC_NEWER": "NEWER"})
    ru.update({"LC_FILTER_TITLE": "Показывать донесения", "LC_IMPORTANT": "Только важное",
               "LC_SF_OPEN": "СВЯЗАТЬСЯ", "LC_BACK": "НАЗАД", "LC_VIEW_CHAT": "ПОКАЗАТЬ ЧАТ",
               "LC_VIEW_LOG": "ЕГО ДОНЕСЕНИЯ", "LC_REPORT": "ЗАПРОСИТЬ ДОКЛАД",
               "LC_END": " ",
               "LC_DR_TITLE": "§HВаши постоянные директивы§!", "LC_OLDER": "РАНЬШЕ",
               "LC_VIEW_TITLE_CHAT": "ПЕРЕПИСКА",
               "LC_VIEW_TITLE_LOG": "ДОНЕСЕНИЯ АГЕНТА",
               "LC_VIEW_SENDING": "§YАгент готовит ответ...§!",
               "LC_VIEW_REPORTING": "§YАгент готовит доклад...§!",
               "LC_NEWER": "ПОЗЖЕ"})
    for k, _, e, r in STAFF:
        en["LC_SF_" + k.upper()], ru["LC_SF_" + k.upper()] = e, r
        en["LC_PM_" + k.upper()] = e.split(" (")[0]
        ru["LC_PM_" + k.upper()] = r.split(" (")[0]
    for k, _, _, e, r in ROSTER.SETTINGS:
        en["LC_S_" + k.upper()], ru["LC_S_" + k.upper()] = e, r
    heads = (("What it decides", "What it does not touch", "Answers to"),
             ("Что решает", "Чего не трогает", "Кому подчиняется"))
    for k, _, e, r in STAFF:
        c = CARDS[k]
        for d, name, h, body in ((en, e, heads[0], c[:3]), (ru, r, heads[1], c[3:])):
            d["LC_CARD_" + k.upper()] = "§Y%s§!\\n\\n" % name + "\\n\\n".join(
                "§H%s§!\\n%s" % (hh, b) for hh, b in zip(h, body))
    for key, *_ in ROSTER.SETTINGS:
        en["LC_SV_" + key.upper()] = ru["LC_SV_" + key.upper()] = "[?lc_s_%s|0]" % key
    en.update({"LC_FILTER_TITLE": "SOURCES", "LC_FILTER_INDUSTRY": "Industry", "LC_FILTER_EVENTS": "Events",
               "LC_FILTER_SUPREME": "Supreme", "LC_FILTER_MARSHALS": "Marshals",
               "LC_S_WAR_MARSHAL_EVERY": "Marshal review in battle (days)",
               "LC_S_MARSHAL_EVERY": "Marshal review when quiet (days)",
               "LC_S_SUPREME_EVERY": "Supreme review at war (days)",
               "LC_S_STAFF_EVERY": "Staff review (days)",
               "LC_S_NAVY_WAR_EVERY": "Admiralty review at war (days)"})
    ru.update({"LC_FILTER_TITLE": "ИСТОЧНИКИ", "LC_FILTER_INDUSTRY": "Промышленность", "LC_FILTER_EVENTS": "События",
               "LC_FILTER_SUPREME": "Ставка", "LC_FILTER_MARSHALS": "Маршалы",
               "LC_S_WAR_MARSHAL_EVERY": "Маршалы в бою (дней)", "LC_S_MARSHAL_EVERY": "Маршалы без боёв (дней)",
               "LC_S_SUPREME_EVERY": "Обзор ставки (дней)", "LC_S_STAFF_EVERY": "Обзор штабов (дней)",
               "LC_S_NAVY_WAR_EVERY": "Обзор флота в войне (дней)",
               "LC_TH_HINT": "ТОЛЬКО AI: передай театр агенту, добавив AI: в начало его имени в игре."})
    en.update({'LC_OFFLINE_HEAD':'Headquarters offline', 'LC_OFFLINE_AGENT':'Connect headquarters to talk to this agent.',
               'LC_OFFLINE_WARNING':'Headquarters offline. Connect the harness to send messages or request reports.',
               'LC_VIEW_QUEUED':'§YRequest queued...§!', 'LC_VIEW_UPDATING':'Updating view...',
               'LC_VIEW_ERROR':'§RDisplay update failed. Reconnect headquarters.§!',
               'LC_TIP_SEND':'Send your message to this agent. Available when headquarters is connected.',
               'LC_TIP_REPORT':'Ask this agent for a report on their situation and recent actions.',
               'LC_TIP_CONTACT':'Open this agent’s conversation, directives and reports.',
               'LC_TIP_DIRECTIVES':'Read every standing directive in full.',
               'LC_TIP_LOG':'Show reports from this agent.', 'LC_TIP_CHAT':'Return to this agent’s conversation.',
               'LC_TIP_PREV':'Previous part of this document.', 'LC_TIP_NEXT':'Next part of this document.',
               'LC_TIP_BACK':'Return to the previous section.', 'LC_TIP_CONTROL':'Connect headquarters to change this setting.'})
    ru.update({'LC_OFFLINE_HEAD':'Штаб не подключён', 'LC_OFFLINE_AGENT':'Для связи с агентом подключи штаб.',
               'LC_OFFLINE_WARNING':'Штаб не подключён. Запусти харнесс для переписки и докладов.',
               'LC_VIEW_QUEUED':'§YЗапрос в очереди...§!', 'LC_VIEW_UPDATING':'Обновляю экран...',
               'LC_VIEW_ERROR':'§RНе удалось обновить экран. Переподключи штаб.§!',
               'LC_TIP_SEND':'Отправить сообщение этому агенту. Нужен подключённый штаб.',
               'LC_TIP_REPORT':'Запросить у агента доклад об обстановке и последних действиях.',
               'LC_TIP_CONTACT':'Открыть переписку, директивы и доклады этого агента.',
               'LC_TIP_DIRECTIVES':'Прочитать все постоянные директивы целиком.',
               'LC_TIP_LOG':'Показать донесения этого агента.', 'LC_TIP_CHAT':'Вернуться к переписке.',
               'LC_TIP_PREV':'Предыдущая часть документа.', 'LC_TIP_NEXT':'Следующая часть документа.',
               'LC_TIP_BACK':'Вернуться в предыдущий раздел.', 'LC_TIP_CONTROL':'Для изменения настройки нужен подключённый штаб.'})
    en.update({'LC_API_SETUP':'API SETUP', 'LC_API_HINT':'API keys: open setup here, or run tools/configure_model.py.',
               'LC_TIP_API_SETUP':'Open secure API setup in a separate window. Offline: python3 tools/configure_model.py. Restart harness to apply.'})
    ru.update({'LC_API_SETUP':'НАСТРОИТЬ API', 'LC_API_HINT':'Ключи API: отдельное окно, или tools/configure_model.py.',
               'LC_TIP_API_SETUP':'Открыть защищённую настройку API. Без штаба: python3 tools/configure_model.py. Затем перезапусти харнесс.'})
    en['LC_SCROLL_HINT'] = 'Use PREVIOUS / NEXT to turn pages.'
    ru['LC_SCROLL_HINT'] = 'Используй НАЗАД / ДАЛЕЕ для перелистывания страниц.'
    en['LC_PLAN_PREV'], en['LC_PLAN_NEXT'] = '< PREVIOUS', 'NEXT >'
    ru['LC_PLAN_PREV'], ru['LC_PLAN_NEXT'] = '< НАЗАД', 'ДАЛЕЕ >'
    for name in MARKERS:
        en["LC_" + name] = marker_source(name, "english")
        ru["LC_" + name] = marker_source(name, "russian")

    def loc(lang, d):
        return "\ufeffl_%s:\n" % lang + "".join(
            ' %s:0 "%s"\n' % (k, v.replace('"', '\\"')) for k, v in d.items())
    # every language the game ships: without a file of its own the panel shows key names and
    # the harness finds no markers, so the others get the English texts
    others = ("french", "german", "polish", "braz_por", "spanish", "japanese", "korean",
              "simp_chinese")
    for lang, d in [("english", en), ("russian", ru)] + [(x, en) for x in others]:
        p = os.path.join(OUT, "localisation", lang)
        os.makedirs(p, exist_ok=True)
        for old in os.listdir(p):
            if old.startswith("le_chonk"):
                os.remove(os.path.join(p, old))
        Path(p, "le_chonk_l_%s.yml" % lang).write_text(loc(lang, d), encoding="utf-8")
    print("built %s: %d elements, %d markers" % (OUT, len(elements), len(MARKERS)))


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", help="Build into an isolated directory instead of the installed mod")
    build(output=parser.parse_args().output)
