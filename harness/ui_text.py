"""Pixel-aware wrapping of localised game text. All operations are offline."""
from functools import lru_cache
from pathlib import Path
import os
import re


@lru_cache(maxsize=16)
def _advances(game_dir, language='en'):
    fonts=Path(game_dir)/'gfx/fonts'
    folders={'ja':'japanese','zh':'chinese','ko':'korean'}
    directory=fonts/folders[language] if language in folders else fonts
    metrics={}
    for suffix in ('','_cryllic'):
        path=directory/('hoi_16mbs'+suffix+'.fnt')
        if not path.exists():continue
        for line in path.read_text().splitlines():
            if line.startswith('char '):
                fields=dict((k,int(v)) for k,v in re.findall(r'(\w+)=\s*(-?\d+)',line))
                metrics[fields['id']]=max(0,fields['xadvance'])
    return metrics


@lru_cache(maxsize=16)
def _metrics(language):
    from .bridge import game_dir
    root=os.environ.get('HOI4_DIR')
    if not root:
        try:
            root=game_dir()
        except RuntimeError:
            return {}
    return _advances(root,language)


def pixel_width(text, language='en'):
    from .modui import SUBST
    metrics=_metrics(language)
    # Account for substitutions performed before the game receives the text.
    text=''.join(SUBST.get(c,c) for c in text)
    return sum(metrics.get(ord(c),16) for c in re.sub(r'[\x11§].','',text))


def ellipsize_coloured(text, width, language='en', max_bytes=None):
    """A single-line preview bounded by glyph width and the actual UTF-8 marker.

    The suffix is explicit, colour escapes stay atomic and a final Cyrillic glyph
    receives the same invisible terminator used by the native marker encoder.
    """
    from .modui import CJK, SUBST, _cjk_fold
    text = ' '.join(str(text or '').split())
    text = ''.join(SUBST.get(ch, ch) for ch in text)
    if language in CJK:
        text = ''.join(_cjk_fold(ch) for ch in text)
    chars = []
    colour = None
    index = 0
    while index < len(text):
        char = text[index]
        if char in ('\x11', '§'):
            if index + 1 < len(text):
                colour = None if text[index + 1] == '!' else text[index + 1]
            index += 2
            continue
        chars.append((char, colour))
        index += 1

    def render(count):
        result, active = [], None
        for char, style in chars[:count]:
            if style != active:
                result.append('\x11' + (style or '!'))
                active = style
            result.append(char)
        if active is not None or (count and ord(chars[count - 1][0]) > 127):
            result.append('\x11!')
        return ''.join(result)

    def fits(value):
        return pixel_width(value, language) <= width and (
            max_bytes is None or len(value.encode('utf-8')) <= max_bytes)

    full = render(len(chars))
    if fits(full):
        return full
    if not fits('...'):
        return ''
    low, high = 0, len(chars)
    while low < high:
        middle = (low + high + 1) // 2
        if fits(render(middle) + '...'):
            low = middle
        else:
            high = middle - 1
    while low and chars[low - 1][0].isspace():
        low -= 1
    return render(low) + '...'


def wrap_coloured(text, width, measure=len):
    """Colour escapes are zero-width atoms; each line can be displayed independently."""
    if width<=0:raise ValueError('width must be positive')
    lines=[];colour=None
    for paragraph in str(text).split('\n'):
        chars=[];i=0
        while i<len(paragraph):
            ch=paragraph[i]
            if ch in ('\x11','§'):
                if i+1<len(paragraph):
                    colour=None if paragraph[i+1]=='!' else paragraph[i+1]
                    i+=2;continue
                i+=1;continue
            chars.append((ch,colour));i+=1
        if not chars:
            lines.append('');continue
        start=0
        while start<len(chars):
            end=start;used=0
            while end<len(chars):
                advance=measure(chars[end][0])
                if end>start and used+advance>width:break
                used+=advance;end+=1
            if end<len(chars):
                spaces=[j for j in range(start,end) if chars[j][0].isspace()]
                if spaces and spaces[-1]>start:end=spaces[-1]
            chunk=chars[start:end]
            while chunk and chunk[-1][0].isspace():chunk.pop()
            out=[];active=None
            for ch,style in chunk:
                if style!=active:
                    out.append('\x11'+(style or '!'));active=style
                out.append(ch)
            if active is not None:out.append('\x11!')
            lines.append(''.join(out))
            start=end
            while start<len(chars) and chars[start][0].isspace():start+=1
    return lines or ['']


def reader_pages(text, width, capacity, language='en', max_lines=240):
    """Scrollable documents split only at safe buffer/line limits, never clipped.

    Reserve colour-reset bytes per line for the native UTF-8 encoder. These parts
    are much larger than the viewport; the game scrolls within each part.
    """
    from .modui import SUBST, CJK, _cjk_fold
    text = ''.join(SUBST.get(ch, ch) for ch in str(text or ''))
    if language in CJK:
        text = ''.join(_cjk_fold(ch) for ch in text)
    lines = wrap_coloured(text, width, lambda value: pixel_width(value, language))
    pages, current, used = [], [], 0
    for line in lines:
        cost = len(line.encode('utf-8')) + 4
        if current and (used + cost > capacity or len(current) >= max_lines):
            while current and not current[-1]:
                current.pop()
            pages.append('\n'.join(current)); current, used = [], 0
            if not line:
                continue
        if cost > capacity:
            raise ValueError('reader capacity is smaller than one rendered line')
        current.append(line); used += cost
    while current and not current[-1]:
        current.pop()
    pages.append('\n'.join(current))
    return pages
