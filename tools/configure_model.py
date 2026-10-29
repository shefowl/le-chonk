#!/usr/bin/env python3
"""Local masked BYOK setup screen. No game access; saving makes no API call."""
import fcntl
import os
from pathlib import Path
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness import credentials


def main():
    import tkinter as tk
    from tkinter import ttk
    path = credentials.config_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = path.parent / '.setup.lock'
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    lock = os.fdopen(lock_fd, 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print('API setup is already open. Switch to its window.'); return
    root = tk.Tk();root.title('Le Chonk · API setup');root.geometry('780x570');root.minsize(740,550)
    root.configure(bg='#25251f')
    style = ttk.Style(root);style.theme_use('clam')
    style.configure('.', background='#25251f', foreground='#e0ddce', fieldbackground='#151713', bordercolor='#716b50')
    style.configure('TButton',padding=8);style.configure('Title.TLabel', font=('Sans',20,'bold'))
    pane = ttk.Frame(root,padding=24);pane.pack(fill='both',expand=True);pane.columnconfigure(1,weight=1)
    ttk.Label(pane,text='LE CHONK  /  API SETUP',style='Title.TLabel').grid(row=0,column=0,columnspan=2,sticky='w',pady=(0,14))
    ttk.Label(pane,text='One model for every role. Credentials stay outside the game and its saves.').grid(row=1,column=0,columnspan=2,sticky='w',pady=(0,16))
    try:
        saved=credentials.load(path); error=''
    except ValueError as exc:
        saved={};error=str(exc)
    provider=tk.StringVar(value=saved.get('provider','mistral'))
    base=tk.StringVar(value=saved.get('base_url',credentials.STUDIO_URL))
    model=tk.StringVar(value=saved.get('model',credentials.LECHONK))
    key=tk.StringVar();status=tk.StringVar(value=error or ('Saved key available. Leave blank to retain it for the same endpoint.' if saved.get('api_key') else 'Enter your API key, then save.'))
    forget=tk.BooleanVar(value=False)
    ttk.Label(pane,text='Provider').grid(row=2,column=0,sticky='w',pady=8)
    combo=ttk.Combobox(pane,textvariable=provider,values=('mistral','custom'),state='readonly');combo.grid(row=2,column=1,sticky='ew')
    ttk.Label(pane,text='API base URL').grid(row=3,column=0,sticky='w',pady=8)
    endpoint=ttk.Entry(pane,textvariable=base);endpoint.grid(row=3,column=1,sticky='ew')
    ttk.Label(pane,text='Model ID').grid(row=4,column=0,sticky='w',pady=8)
    ttk.Entry(pane,textvariable=model).grid(row=4,column=1,sticky='ew')
    ttk.Label(pane,text='API key').grid(row=5,column=0,sticky='w',pady=8)
    secret=ttk.Entry(pane,textvariable=key,show='•');secret.grid(row=5,column=1,sticky='ew')
    ttk.Checkbutton(pane,text='Forget the saved key (environment key may still be used)',variable=forget).grid(row=6,column=1,sticky='w',pady=8)
    ttk.Label(pane,text='Mistral Studio: direct API, default model mistral-large-4.\nCustom: OpenAI-compatible /chat/completions endpoint, including local servers.\nBlank key retains a saved key only for the same provider and endpoint.',wraplength=700).grid(row=7,column=0,columnspan=2,sticky='w',pady=12)
    ttk.Label(pane,textvariable=status,wraplength=700,foreground='#ddc878').grid(row=8,column=0,columnspan=2,sticky='w',pady=10)
    ttk.Label(pane,text='Saved to '+str(path)+'\nChanges apply on the next harness start; active calls keep their current configuration.',wraplength=700).grid(row=9,column=0,columnspan=2,sticky='w',pady=10)
    def selection(_=None):
        key.set('');forget.set(False)
        base.set(credentials.STUDIO_URL if provider.get()=='mistral' else '')
        model.set(credentials.LECHONK if provider.get()=='mistral' else '')
        endpoint.configure(state='readonly' if provider.get()=='mistral' else 'normal')
        status.set('Provider changed. Enter a key for this endpoint; other provider keys are never copied.')
    combo.bind('<<ComboboxSelected>>',selection)
    endpoint.configure(state='readonly' if provider.get()=='mistral' else 'normal')
    def values():
        return credentials.validate(dict(provider=provider.get(),base_url=base.get(),model=model.get(),api_key=key.get()))
    def save():
        try:
            credentials.save(values(),path,forget_key=forget.get())
        except (ValueError,OSError) as exc:
            status.set(str(exc) if isinstance(exc,ValueError) else 'Cannot save settings. Check directory permissions.');return
        key.set('');forget.set(False)
        status.set('Saved securely (mode 600). Restart the harness to apply. No API request was made.')
    busy=False
    def verify():
        nonlocal busy
        if busy:return
        try:
            data=values();old=credentials.load(path)
            if not data['api_key'] and not forget.get() and all(data[k]==old.get(k) for k in ('provider','base_url')):
                data['api_key']=old.get('api_key','')
            if not data['api_key'] and data['provider']=='mistral':
                from harness.llm import load_env
                load_env();data['api_key']=os.environ.get('MISTRAL_API_KEY','')
        except (ValueError,OSError) as exc:
            status.set(str(exc) if isinstance(exc,ValueError) else 'Cannot read configuration.');return
        if not data['api_key'] and data['provider']=='mistral':
            status.set('Enter a Studio API key before checking access.');return
        busy=True;check.configure(state='disabled');status.set('Checking model access with GET /models (no completion, no game access)…')
        def done(message):
            nonlocal busy
            busy=False;check.configure(state='normal');status.set(message)
        def worker():
            import requests
            try:
                headers={'Authorization':'Bearer '+data['api_key']} if data['api_key'] else {}
                with requests.get(data['base_url']+'/models',headers=headers,timeout=20,allow_redirects=False) as response:
                    if response.status_code!=200:message='Access check returned HTTP %d. Check key and endpoint.'%response.status_code
                    else:
                        ids={r.get('id') for r in response.json().get('data',[]) if isinstance(r,dict)}
                        message='Access confirmed: model is listed.' if data['model'] in ids else 'Connected, but the selected model is not listed for this key.'
            except Exception:
                message='Access check failed. Check network, endpoint and API credentials.'
            root.after(0,done,message)
        threading.Thread(target=worker,daemon=True).start()
    buttons=ttk.Frame(pane);buttons.grid(row=10,column=0,columnspan=2,sticky='e',pady=12)
    check=ttk.Button(buttons,text='Check model access',command=verify);check.pack(side='left',padx=6)
    ttk.Button(buttons,text='Save settings',command=save).pack(side='left',padx=6)
    ttk.Button(buttons,text='Close',command=root.destroy).pack(side='left',padx=6)
    root.mainloop();lock.close()


if __name__=='__main__':main()
