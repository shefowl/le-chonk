"""Owner-only BYOK configuration. Credentials never enter game variables or journals."""
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / 'config' / 'llm.json'
STUDIO_URL = 'https://api.mistral.ai/v1'
LECHONK = 'mistral-large-4'
CHEAP = {'haiku':'anthropic/claude-haiku-5.5', 'deepseek':'deepseek/deepseek-v4.1-flash',
         'mimo':'xiaomi/mimo-v2.6-flash', 'haiku-sub':'claude-haiku-5-5'}
SUBSCRIPTION = {'haiku-sub'}


def config_path():
    return Path(os.environ.get('LECHONK_CONFIG', PATH)).expanduser()


def validate(data):
    if not isinstance(data, dict) or set(data) - {'provider','base_url','model','api_key'}:
        raise ValueError('Configuration must contain provider, base_url, model and api_key only')
    provider = data.get('provider', 'mistral')
    if provider not in ('mistral','custom'):
        raise ValueError('Choose mistral or custom provider')
    model = data.get('model', LECHONK if provider == 'mistral' else '')
    key = data.get('api_key', '')
    if not isinstance(model,str) or not model.strip() or len(model)>200 or any(ord(c)<32 for c in model):
        raise ValueError('Enter a model ID without control characters (up to 200 characters)')
    if not isinstance(key,str) or len(key)>4096 or any(ord(c)<32 for c in key):
        raise ValueError('API key must be one line (up to 4096 characters)')
    base = data.get('base_url', STUDIO_URL if provider == 'mistral' else '')
    if not isinstance(base,str) or len(base)>2048 or any(c.isspace() for c in base):
        raise ValueError('Enter a valid API base URL')
    try:
        url = urlsplit(base)
        _ = url.port
    except ValueError:
        raise ValueError('Invalid API base URL') from None
    if (not url.hostname or url.username is not None or url.password is not None or url.query or url.fragment
            or url.scheme not in ('http','https')
            or (url.scheme == 'http' and url.hostname not in ('localhost','127.0.0.1','::1'))):
        raise ValueError('Use HTTPS, or localhost HTTP; no credentials, query or fragment in URL')
    base = base.rstrip('/')
    if provider == 'mistral' and base != STUDIO_URL:
        raise ValueError('Mistral Studio uses https://api.mistral.ai/v1; choose custom for another endpoint')
    return dict(provider=provider, base_url=base, model=model.strip(), api_key=key.strip())


def load(path=None):
    path = Path(path) if path is not None else config_path()
    if path.is_symlink():
        raise ValueError('Credential file must not be a symlink')
    try:
        with path.open(encoding='utf-8') as f:
            if os.fstat(f.fileno()).st_mode & 0o077:
                raise ValueError('Credential file is readable by others; run chmod 600 on it')
            raw = f.read(16385)
        if len(raw)>16384:
            raise ValueError('Credential file is too large')
        return validate(json.loads(raw))
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, UnicodeError):
        raise ValueError('Invalid credential JSON; fix it or open the setup screen') from None


def save(data, path=None, *, forget_key=False):
    path = Path(path) if path is not None else config_path()
    data = validate(data)
    if path.is_symlink():
        raise ValueError('Credential file must not be a symlink')
    try:
        previous = load(path)
    except ValueError:
        previous = {}  # Explicit save repairs malformed/private-mode files without retaining secrets.
    if (not data['api_key'] and not forget_key and previous.get('provider') == data['provider']
            and previous.get('base_url') == data['base_url']):
        data['api_key'] = previous.get('api_key','')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix='.llm-', dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            os.fchmod(f.fileno(),0o600)
            json.dump(data,f,indent=2); f.write('\n');f.flush();os.fsync(f.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name): os.unlink(name)


def resolve_profile(args, config=None):
    config = load() if config is None else config
    if args.models == 'lechonk':
        if args.provider not in (None,'mistral') or any(
                m not in (None,LECHONK) for m in (args.supreme_model,args.theater_model)):
            raise ValueError('lechonk requires Mistral Studio and mistral-large-4 for every role; use configured for custom models')
        args.provider = 'mistral'
        args.supreme_model = args.theater_model = LECHONK
    elif args.models == 'configured':
        if not config: raise ValueError('Configure BYOK first: python3 tools/configure_model.py')
        if args.provider is not None and args.provider != config['provider']:
            raise ValueError('configured profile uses the provider selected in BYOK setup')
        args.provider = config['provider']
        args.supreme_model = args.supreme_model or config['model']
        args.theater_model = args.theater_model or config['model']
    else:
        args.provider = args.provider or ('claude' if args.models in SUBSCRIPTION else 'openrouter')
        args.supreme_model = args.supreme_model or CHEAP[args.models]
        args.theater_model = args.theater_model or CHEAP[args.models]
    return args


def verify_start(args, info):
    """Assert loaded campaign identity after Gateway's bounded clock calibration."""
    country=getattr(args,'expect_country',None)
    day=getattr(args,'expect_start_day',None)
    if country and info.get('my_tag') != country:
        raise ValueError('Loaded country is %s; expected %s. Game remains paused.'%(info.get('my_tag'),country))
    if day and not str(info.get('date','')).startswith(day+'.'):
        raise ValueError('Loaded date is %s; expected %s. Game remains paused.'%(info.get('date'),day))
