"""Native HOI4 metal, bevels and department badges; deterministic DDS output."""
import os
from pathlib import Path
from PIL import Image, ImageDraw, ImageEnhance

BRASS = (164, 146, 102, 255)
GAME = Path(os.environ.get('HOI4_DIR', Path.home()/'.local/share/Steam/steamapps/common/Hearts of Iron IV'))


def nine_slice(source, size, edge=12):
    """Keep vanilla rivets and bevel thickness when resizing a panel."""
    w, h = size; sw, sh = source.size
    edge = min(edge, w//2, h//2, sw//2, sh//2)
    out = Image.new('RGBA', size)
    xs, ys = (0, edge, sw-edge, sw), (0, edge, sh-edge, sh)
    dx, dy = (0, edge, w-edge, w), (0, edge, h-edge, h)
    for row in range(3):
        for col in range(3):
            tile=source.crop((xs[col],ys[row],xs[col+1],ys[row+1]))
            tile=tile.resize((dx[col+1]-dx[col],dy[row+1]-dy[row]),Image.Resampling.LANCZOS)
            out.alpha_composite(tile,(dx[col],dy[row]))
    return out


def build(output, width=1040, height=620):
    directory=Path(output)/'gfx/interface/le_chonk';directory.mkdir(parents=True,exist_ok=True)
    sprites={}
    def source(path):
        with Image.open(GAME/'gfx/interface'/path) as im:return im.convert('RGBA')
    def save(name, image, frames=1):
        path=directory/(name+'.dds')
        if name.startswith('emblem_') or name in ('open','wait'):image.save(path)
        else:image.save(path,pixel_format='DXT5')
        sprites['GFX_lc_'+name]=('gfx/interface/le_chonk/'+path.name,frames)
    metal=source('tiles/tiled_bg.dds');plain=source('tiles/tiled_plain_bg.dds')
    header=source('header_bg.dds')
    window=nine_slice(metal,(width,height),24)
    window.alpha_composite(nine_slice(header,(width-8,66),12),(4,4))
    # A framed narrow rail, same steel as the game, rather than flat dashboard chrome.
    rail=nine_slice(plain,(176,height-92),10)
    window.alpha_composite(rail,(10,82))
    save('window',window)
    for name,w,h in (('row',808,46),('card',548,408),('feed',604,414),
                       ('filter',188,414),('chat',808,224),('directive',808,106),
                       ('idea',808,128),('war',808,88),('settings',396,392),
                       ('input',554,34),('status',808,46),('theatre',808,42),('reader',808,394)):
        im=nine_slice(plain,(w,h),9)
        if name in ('card','filter','settings'):
            im.alpha_composite(nine_slice(header,(w-6,46),9),(3,3))
        save(name,im)
    base=source('button_148x34.dds');green=source('button_123x34.dds')
    for name,w,h in (('report',148,34),('button',112,34),('wide',224,34),('mode',152,34),
                       ('nav',156,42),('staff',224,38),('switch',80,28),('check',24,24),('step',28,28)):
        for selected in (False,True):
            sheet=Image.new('RGBA',(w*3,h))
            for frame,brightness in enumerate((1.0,1.22,.72)):
                tile=nine_slice(green if selected else base,(w,h),8)
                tile=ImageEnhance.Brightness(tile).enhance(brightness)
                if selected:
                    ImageDraw.Draw(tile).line((9,h-6,w-10,h-6),fill=BRASS,width=1)
                sheet.alpha_composite(tile,(w*frame,0))
            save(name+('_active' if selected else ''),sheet,3)
    check=source('generic_checkbox.dds');cw=check.width//2
    for on in (False,True):
        tile=check.crop((cw*int(on),0,cw*(int(on)+1),check.height))
        save('check_on' if on else 'check_off',tile.resize((24,24),Image.Resampling.LANCZOS))
    icons={'planner':'topbar/toolbar/ledger_button.dds',
           'supreme':'topbar/toolbar/staff_office_button.dds',
           'events':'topbar/toolbar/diplomacy_button.dds',
           'politics':'topbar/toolbar/topbar_decisionview_button.dds',
           'industry':'topbar/toolbar/production_button.dds',
           'army':'topbar/armyoverview_button.dds','air':'topbar/airoverview_button.dds',
           'admiralty':'topbar/navyoverview_button.dds','marshals':'topbar/toolbar/staff_office_button.dds'}
    for name,path in icons.items():
        im=source(path);im=im.crop((0,0,im.width//2,im.height))
        tile=Image.new('RGBA',(38,32));im.thumbnail((38,32),Image.Resampling.LANCZOS)
        tile.alpha_composite(im,((38-im.width)//2,(32-im.height)//2));save('emblem_'+name,tile)
    # Vanilla round bezel, with the staff cap identifying our command desk.
    ring=source('topbar/armyoverview_button.dds').crop((0,0,38,38))
    badge=source('topbar/toolbar/staff_office_button.dds').crop((7,5,43,34)).resize((25,21),Image.Resampling.LANCZOS)
    mask=Image.new('L',badge.size);ImageDraw.Draw(mask).ellipse((0,0,24,20),fill=255)
    badge.putalpha(mask)
    opened=Image.new('RGBA',(46*3,46))
    for frame,brightness in enumerate((1.,1.22,.72)):
        tile=ring.copy()
        ImageDraw.Draw(tile).ellipse((5,5,32,32),fill=(27,29,26,255))
        tile.alpha_composite(badge,(7,9));tile=ImageEnhance.Brightness(tile).enhance(brightness)
        opened.alpha_composite(tile.resize((46,46),Image.Resampling.LANCZOS),(frame*46,0))
    save('open',opened,3)
    # Real-time native animation; keeps moving while the game is paused.
    import math
    sheet=Image.new('RGBA',(20*12,20))
    for frame in range(12):
        tile=Image.new('RGBA',(80,80));draw=ImageDraw.Draw(tile)
        for tick in range(12):
            angle=tick*math.tau/12-math.pi/2
            brightness=0.28+0.72*((tick-frame)%12)/11
            colour=tuple(int(value*brightness) for value in BRASS[:3])+(255,)
            x,y=40+27*math.cos(angle),40+27*math.sin(angle)
            draw.ellipse((x-4,y-4,x+4,y+4),fill=colour)
        sheet.alpha_composite(tile.resize((20,20),Image.Resampling.LANCZOS),(frame*20,0))
    save('wait',sheet,12)
    return sprites
