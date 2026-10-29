#!/usr/bin/env python3
"""Offline preview of generated Clausewitz GUI using its DDS textures and game bitmap fonts.

This does not connect to the game. Native widget input/hover must still be checked in game.
"""
import argparse
import json
import re
from pathlib import Path
from PIL import Image, ImageDraw, ImageChops

GAME = Path.home()/'.local/share/Steam/steamapps/common/Hearts of Iron IV'
# Palette from the installed interface/core.gfx, global + hoi_16mbs overrides.
COLOURS = {'H':(238,201,35,255),'Y':(238,201,35,255),'G':(86,172,91,255),
           'R':(222,86,70,255),'C':(35,206,255,255),'W':(255,255,255,255),
           '2':(81,112,243,255),'6':(119,204,186,255),'7':(153,209,153,255),
           '8':(204,163,51,255),'9':(252,169,125,255),'4':(90,190,231,255),
           'L':(195,176,145,255),'g':(175,175,175,255)}
DEFAULT=(255,255,255,255)



def parse(source):
    tokens=re.findall(r'"(?:\\.|[^"\\])*"|#[^\n]*|[{}=<>]|[^\s{}=<>]+',source)
    tokens=[t for t in tokens if not t.startswith('#')]
    pos=0
    def value():
        nonlocal pos
        t=tokens[pos];pos+=1
        if t=='{':
            pairs=[]
            while tokens[pos]!='}':
                key=tokens[pos];pos+=1
                op=tokens[pos];pos+=1
                if op not in ('=','>','<'):raise ValueError((key,op))
                pairs.append((key,op,value()))
            pos+=1
            return pairs
        return t[1:-1] if t.startswith('"') else t
    pairs=[]
    while pos<len(tokens):
        key=tokens[pos];pos+=1
        if tokens[pos]!='=':raise ValueError(key)
        pos+=1;pairs.append((key,'=',value()))
    return pairs


def get(node,key,default=None):
    return next((v for k,op,v in node if k==key),default)


def condition(node,flags,variables):
    for k,op,v in node:
        if k=='NOT':ok=not condition(v,flags,variables)
        elif k=='OR':ok=any(condition([entry],flags,variables) for entry in v)
        elif k=='AND':ok=condition(v,flags,variables)
        elif k=='has_country_flag':ok=v in flags
        elif k=='always':ok=v=='yes'
        elif k=='check_variable':
            ok=True
            for var,comparison,want in v:
                actual=float(variables.get(var,0));want=float(variables.get(want,want))
                ok=ok and ({'=':actual==want,'>':actual>want,'<':actual<want}[comparison])
        else:raise ValueError('unknown trigger '+k)
        if not ok:return False
    return True


class BitmapFont:
    def __init__(self,name):
        self.glyphs={};self.line=20
        for suffix in ('','_cryllic'):
            path=GAME/'gfx/fonts'/(name+suffix+'.fnt')
            if not path.exists():continue
            atlas=None;chars=[]
            for line in path.read_text().splitlines():
                if line.startswith('common '):self.line=max(20,int(re.search(r'lineHeight=(\d+)',line)[1]))
                if line.startswith('page '):
                    atlas_path=path.parent/re.search(r'file="([^"]+)"',line)[1]
                    if not atlas_path.exists():atlas_path=path.with_suffix('.dds')
                    with Image.open(atlas_path) as source:atlas=source.convert('RGBA')
                if line.startswith('char '):chars.append(dict((k,int(v)) for k,v in re.findall(r'(\w+)=\s*(-?\d+)',line)))
            if atlas is None and path.with_suffix('.dds').exists():
                with Image.open(path.with_suffix('.dds')) as source:atlas=source.convert('RGBA')
            for g in chars:
                self.glyphs[g['id']]=(atlas,g)
    def advance(self,ch):
        item=self.glyphs.get(ord(ch),self.glyphs.get(ord('?')))
        return item[1]['xadvance'] if item else 8
    def width(self,text):return sum(self.advance(ch) for ch in re.sub(r'[\x11§].','',text))
    def lines(self,text,width):
        out=[]
        for paragraph in text.split('\n'):
            line=''
            for word in paragraph.split(' '):
                candidate=(line+' '+word) if line else word
                if line and self.width(candidate)>width:
                    out.append(line);line=word
                else:line=candidate
            out.append(line)
        return out
    def draw(self,image,text,xy,width,height,centre=False):
        lines=self.lines(text,width)
        layer=Image.new('RGBA',(width,height));colour=DEFAULT;y=0
        for line in lines:
            x=max(0,(width-self.width(line))//2) if centre else 0
            i=0
            while i<len(line):
                ch=line[i]
                if ch in ('\x11','§') and i+1<len(line):
                    colour=COLOURS.get(line[i+1],DEFAULT);i+=2;continue
                item=self.glyphs.get(ord(ch),self.glyphs.get(ord('?')))
                if item:
                    atlas,g=item
                    glyph=atlas.crop((g['x'],g['y'],g['x']+g['width'],g['y']+g['height']))
                    ink=ImageChops.multiply(glyph,Image.new('RGBA',glyph.size,colour));ink.putalpha(glyph.getchannel('A'))
                    layer.alpha_composite(ink,(int(x+g['xoffset']),int(y+g['yoffset'])))
                    x+=g['xadvance']
                i+=1
            y+=self.line
        image.alpha_composite(layer,xy)
        return (any(self.width(line) > width for line in lines) or
                (y>height and any(l.strip() for l in lines[max(0,height//self.line):])))


def localisations(build,lang):
    path=build/'localisation'/lang/('le_chonk_l_'+lang+'.yml')
    loc={}
    for line in path.read_text('utf-8-sig').splitlines():
        m=re.match(r'\s*(\w+):\d+ "(.*)"$',line)
        if m:loc[m[1]]=m[2].replace(r'\"','"').replace(r'\n','\n')
    return loc


def sample_texts(ru=False):
    roles=['PLANNER','SUPREME','EVENTS','POLITICS','INDUSTRY','ARMY','AIR','ADMIRALTY']
    texts={'HEAD':'1940.10.19  |  '+('Война: GER, ITA и союзники' if ru else 'War: GER, ITA and allies'),
           'FD_PAGE':'Страница 1 / 23' if ru else 'Page 1 / 23',
           'PL_REVIEW':'План от 1940.10.19   /   Пересмотр: 1941.01.20' if ru else 'Plan: 1940.10.19   /   Next review: 1941.01.20',
           'PL_IDEA':'Покупать топливо и действовать. Нефтяные контракты сохраняются; штабы действуют по военной обстановке, не ожидая заполнения хранилищ. Удержать Гибралтар и Суэц, закрыть разрывы фронта в Восточной Африке. Наступление в пустыне — только по решению ставки.' if ru else 'Buy the fuel, then fight. Oil deals stay at full levels. Armies, fighters and fleets act on military triggers. Hold Gibraltar and Suez; close the gaps in East Africa. The desert drive waits for the supreme command.',
           'AG_ST':'Играет агент  /  Последний ход: 1940.10.19' if ru else 'Agent in command  /  Last turn: 1940.10.19',
           'DR_1':'1. Сначала снаряжение действующих дивизий.' if ru else '1. Equip the divisions in the field first.',
           'DR_2':'2. Новые корабли — по запросу адмиралтейства.' if ru else '2. New ships follow the Admiralty’s requests.',
           'DR_3':'', 'DR_PAGE':'1 / 2',
           'DR_TEXT':('1. Сначала снаряжение действующих дивизий.\n\n2. Не меняй стратегические приоритеты без доклада ставке.\n\n3. Согласовывай заказы авиации со штабом ВВС.' if ru else
                      '1. Equip the divisions in the field first.\n\n2. Report to supreme command before changing strategic priorities.\n\n3. Coordinate aircraft orders with the air staff.'),
           'SENT':('Сообщение передано агенту.' if ru else 'Message delivered to the agent.'),
           'TH_MODE':'Всеми театрами управляют маршалы' if ru else 'All theatres delegated to field marshals',
           'FL_MODE':'Всеми флотами управляет адмиралтейство' if ru else 'All fleets delegated to the Admiralty',
           'SET_NOTE':'Сейчас: сухопутная война. Ходы в режиме «МИР И БОИ» ставят игру на паузу.' if ru else 'Now: land war. PEACE / LAND WAR turns pause the game.'}
    statuses=(['Ожидает следующего пересмотра плана.','Удержать Гибралтар и Суэц. Закрыть разрывы фронта.','Нет новых событий.','Текущий фокус выполняется.','Производство работает. Запасы топлива растут.','Пополнения направлены в действующие армии.','Истребители прикрывают Матрух и Верхний Нил.','Открыты вопросы по патрулям в Средиземном море.'] if ru else
              ['Waiting for the next plan review.','Hold Gibraltar and Suez. Close the gaps in East Africa.','No new events to resolve.','The current focus is running.','Production is running. Fuel stocks are rising.','Reinforcements assigned to the field armies.','Fighters cover Matrouh and the Upper Nile.','Mediterranean patrol assignments need attention.'])
    for key,status in zip(roles,statuses):texts['ST_'+key]=status
    theatres=[('Home Islands','Alan Brooke','GER'),('Middle East','Claude Auchinleck','ITA'),('India','William Slim','SIA'),('East Africa','Bernard Montgomery','ITA')]
    for i in range(1,9):
        texts['TH_'+str(i)]=(' / '.join(theatres[i-1])+(' / удержание фронта' if ru else ' / holding the line')) if i<=4 else ''
    texts['PL_WAR_1']=('ГЕРМАНИЯ И СОЮЗНИКИ  /  ПОДГОТОВКА\nОсвободить Бельгию, Нидерланды, Францию и Польшу. Переход БЭК — после подтверждения маршрута адмиралтейством.' if ru else 'GERMANY & ALLIES  /  PREPARING\nLiberate Belgium, the Netherlands, France and Poland. BEF crossing waits for a convoy route from the Admiralty.')
    texts['PL_WAR_2']=('ИТАЛИЯ И СОЮЗНИКИ  /  ГОТОВО\nУдержать Суэц и Средиземноморье. Вернуть Ливию и Итальянскую Восточную Африку; готовность нефтяных поставок проверяет ставка.' if ru else 'ITALY & ALLIES  /  READY\nHold Suez and the Mediterranean. Retake Libya and Italian East Africa; the supreme command reviews oil deliveries.')
    texts['PL_WAR_3']=''
    entries=[]
    for i in range(4):
        role=('ШТАБ ВВС','АРМЕЙСКИЙ ШТАБ','LE CHONK','АДМИРАЛТЕЙСТВО')[i] if ru else ('AIR STAFF','ARMY STAFF','LE CHONK','ADMIRALTY')[i]
        body=statuses[(6,5,1,7)[i]]
        entries.append('§H'+role+'§!  19.10\n'+body+'\n')
    texts['FEED']='\n'.join(entries)
    texts['CHAT']=('§HТЫ§!\nКак растёт производство и хватает ли топлива?\n\n§HПРОМЫШЛЕННОСТЬ§!\nЛинии снабжают действующие дивизии. Нефтяные контракты сохранены; запас топлива растёт. Проверю доставку по каждому поставщику и доложу об изменениях.\n\n§HТЫ§!\nПока придерживайся текущих приоритетов.' if ru else '§HYOU§!\nHow is production doing, and do we have enough fuel?\n\n§HINDUSTRY§!\nThe lines are equipping the field divisions. Oil deals stay in place and fuel stocks are rising. I will check each supplier’s deliveries and report any changes.\n\n§HYOU§!\nKeep the current priorities for now.')
    return texts


def render(build,output,lang='english',tab='agents',card=4,state=None):
    ui=get(parse((build/'interface/le_chonk.gui').read_text()),'guiTypes')
    window=next(v for k,op,v in ui if k=='containerWindowType' and get(v,'name')=='lc_panel_window')
    script=get(parse((build/'common/scripted_guis/le_chonk_scripted_gui.txt').read_text()),'scripted_gui')
    panel=get(script,'lc_panel');triggers=get(panel,'triggers')
    gfx=get(parse((build/'interface/le_chonk.gfx').read_text()),'spriteTypes')
    sprites={get(v,'name'):(get(v,'texturefile'),int(get(v,'noOfFrames','1'))) for k,op,v in gfx}
    state = state or {}
    loc=localisations(build,lang);dynamic=sample_texts(lang=='russian')
    if state.get('connected') is False:
        dynamic = {}
    dynamic.update(state.get('texts', {}))
    if 'PL_TEXT' not in dynamic and dynamic:
        dynamic['PL_TEXT'] = dynamic.get('PL_IDEA', '') + '\n\n' + '\n\n'.join(
            dynamic.get('PL_WAR_%d' % i, '') for i in range(1, 4))
    for name,value in dynamic.items():loc['LC_'+name]=value
    flags={'lc_panel_open'} | ({'lc_tab_'+tab} if tab!='agents' else set())
    if tab == 'directives':
        flags.discard('lc_tab_directives')
        flags.update(('lc_tab_agent', 'lc_directives_open'))
    if state.get('connected') is not False:flags.add('lc_online')
    flags.update(state.get('flags', []))
    variables={'lc_card':card,'lc_agent':card,'lc_s_speed_peace':2,'lc_s_speed_war':3,'lc_s_war_marshal_every':1,
               'lc_s_marshal_every':7,'lc_s_supreme_every':5,'lc_s_staff_every':30,'lc_s_navy_war_every':7,
               'lc_pm_army':2}
    variables.update(state.get('variables', {}))
    image=Image.new('RGBA',(1040,620),(20,24,25,255));fonts={};overflow=[]
    def draw_items(items, px=0, py=0):
        for kind,op,node in items:
            if kind == 'containerWindowType':
                c_name = get(node, 'name')
                trigger = get(triggers, c_name + '_visible')
                if trigger is not None and not condition(trigger, flags, variables):
                    continue
                pos = get(node, 'position', [])
                cx = px + int(get(pos, 'x', '0'))
                cy = py + int(get(pos, 'y', '0'))
                size = get(node, 'size', [])
                cw = int(get(size, 'width', get(size, 'x', '0')))
                ch = int(get(size, 'height', get(size, 'y', '0')))
                has_scroll = get(node, 'verticalScrollbar') is not None
                draw_items(node, cx, cy)
                if has_scroll and cw and ch:
                    d = ImageDraw.Draw(image)
                    sx = cx + cw - 16
                    d.rectangle((sx, cy, sx + 12, cy + ch - 1), fill=(17, 17, 15), outline=(75, 74, 65))
                    thumb = min(ch - 24, max(24, int(ch * 0.4)))
                    d.rectangle((sx + 1, cy + 12, sx + 11, cy + 12 + thumb), fill=(102, 101, 88), outline=(154, 149, 125))
                    d.polygon((sx + 2, cy + 8, sx + 10, cy + 8, sx + 6, cy + 3), fill=(177, 166, 133))
                    d.polygon((sx + 2, cy + ch - 8, sx + 10, cy + ch - 8, sx + 6, cy + ch - 3), fill=(177, 166, 133))
                continue
            if kind not in ('iconType','buttonType','instantTextBoxType','editBoxType','background'):continue
            name=get(node,'name');trigger=get(triggers,name+'_visible')
            if trigger is not None and not condition(trigger,flags,variables):continue
            pos=get(node,'position',[]);x=px+int(get(pos,'x','0'));y=py+int(get(pos,'y','0'))
            sprite=get(node,'spriteType',get(node,'quadTextureSprite'))
            size=get(node,'size',[]);width=int(get(size,'x','0'));height=int(get(size,'y','0'))
            if sprite in sprites:
                path,frames=sprites[sprite]
                with Image.open(build/path) as src:
                    tile=src.convert('RGBA');width=tile.width//frames;height=tile.height
                    image.alpha_composite(tile.crop((0,0,width,height)),(x,y))
            elif kind=='buttonType' and sprite=='GFX_closebutton':
                d=ImageDraw.Draw(image);d.line((x+5,y+5,x+21,y+21),fill=DEFAULT,width=2);d.line((x+21,y+5,x+5,y+21),fill=DEFAULT,width=2)
                continue
            fontname=get(node,'buttonFont',get(node,'font','hoi_16mbs'))
            if fontname not in fonts:fonts[fontname]=BitmapFont(fontname)
            font=fonts[fontname]
            key=get(node,'buttonText',get(node,'text',''))
            text=loc.get(key,key)
            text=text.rstrip(' \t')
            text=re.sub(r'\[\?([^|]+)\|0\]',lambda m:str(variables.get(m[1],0)),text)
            if kind=='buttonType':
                if font.draw(image,text,(x+4,y+max(0,(height-font.line)//2)),max(1,width-8),max(1,height),True):overflow.append(name)
            elif kind in ('instantTextBoxType','editBoxType'):
                width=int(get(node,'maxWidth',str(width)));height=int(get(node,'maxHeight',str(height)))
                if width and height and font.draw(image,text,(x,y),width,height,get(node,'format')=='centre'):
                    overflow.append(name)

    draw_items(window)
    output.parent.mkdir(parents=True,exist_ok=True)
    image.save(output)
    return overflow


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('build',type=Path);ap.add_argument('output',type=Path)
    ap.add_argument('--snapshot', type=Path, help='Offline JSON with texts, variables and flags from a saved fixture')
    args=ap.parse_args();warnings={}
    state = json.loads(args.snapshot.read_text()) if args.snapshot else None
    for lang in ('english','russian'):
        for tab in ('agents','staff','theatres','reports','plan','settings','agent','directives'):
            warnings[lang+'/'+tab]=render(args.build,args.output/(lang+'-'+tab+'.png'),lang,tab,state=state)
    print(json.dumps(warnings,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
