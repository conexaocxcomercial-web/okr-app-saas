import os
import time
from uuid import uuid4
from typing import List, Optional, Dict
from dataclasses import dataclass, field
from datetime import date, datetime
import pandas as pd
import numpy as np
from sqlalchemy import create_engine, text
from nicegui import ui, app
import plotly.express as px
from io import BytesIO

# --- 1. CONFIGURAÇÃO E CONSTANTES ---
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///okr_local.db")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

CORES_STATUS = {
    "Concluído": "#bef533",
    "Em Andamento": "#7371ff",
    "Pausado": "#ffd166",
    "Não Iniciado": "#ff5a34"
}

CORES_PRAZO = {
    "Atrasado": "#ff5a34",
    "Urgente (7 dias)": "#ff9f1c",
    "Atenção (30 dias)": "#ffd166",
    "No Prazo": "#7371ff",
    "Concluído": "#e0e0e0",
    "Sem Prazo": "#f0f2f6"
}

# --- 2. PERSISTÊNCIA (SQLAlchemy) ---
class Persistence:
    def __init__(self, db_url):
        self.engine = create_engine(db_url)
        self._init_db()

    def _init_db(self):
        with self.engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS okrs (
                    departamento TEXT, objetivo TEXT, kr TEXT, 
                    tarefa TEXT, status TEXT, responsavel TEXT, prazo TEXT,
                    avanco REAL, alvo REAL, cliente TEXT
                )
            """))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS users (
                    username TEXT PRIMARY KEY,
                    password TEXT,
                    name TEXT,
                    cliente TEXT
                )
            """))

    def login(self, username, password):
        with self.engine.connect() as conn:
            res = conn.execute(text("SELECT * FROM users WHERE username=:u AND password=:p"), 
                               {'u': username, 'p': password}).mappings().first()
            return dict(res) if res else None

    def create_user(self, username, password, name, client):
        with self.engine.begin() as conn:
            check = conn.execute(text("SELECT 1 FROM users WHERE username=:u"), {'u': username}).first()
            if check: return False, "Usuário já existe"
            
            conn.execute(text("INSERT INTO users (username, password, name, cliente) VALUES (:u, :p, :n, :c)"),
                         {'u': username, 'p': password, 'n': name, 'c': client})
            return True, "Criado com sucesso"

    def load_okrs(self, client):
        try:
            with self.engine.connect() as conn:
                return pd.read_sql(text("SELECT * FROM okrs WHERE cliente = :c"), conn, params={'c': client})
        except:
            return pd.DataFrame()

    def save_okrs(self, df, client):
        with self.engine.begin() as conn:
            conn.execute(text("DELETE FROM okrs WHERE cliente = :c"), {'c': client})
            if not df.empty:
                df['cliente'] = client 
                df.to_sql('okrs', conn, if_exists='append', index=False)

db = Persistence(DATABASE_URL)

# --- 3. DOMÍNIO (Classes Lógicas) ---
@dataclass
class Task:
    id: str = field(default_factory=lambda: str(uuid4()))
    description: str = ""
    status: str = "Não Iniciado"
    responsible: str = ""
    deadline: Optional[str] = None
    _callback: Optional[callable] = None

    def trigger_update(self):
        if self._callback: self._callback()

@dataclass
class KeyResult:
    name: str
    target: float = 1.0
    current: float = 0.0
    tasks: List[Task] = field(default_factory=list)
    # Correção do Bug: Estado de expansão salvo no objeto
    expanded: bool = False 
    _callback: Optional[callable] = None

    @property
    def progress_pct(self):
        if self.target == 0: return 1.0 if self.current >= 0 else 0.0
        return min(max(self.current / self.target, 0.0), 1.0)

    def add_task(self, t: Task):
        t._callback = self.notify
        self.tasks.append(t)
        # Ao adicionar tarefa, garante que continua expandido
        self.expanded = True 
        self.notify()
    
    def remove_task(self, t: Task):
        if t in self.tasks:
            self.tasks.remove(t)
            self.notify()

    def notify(self):
        if self._callback: self._callback()

@dataclass
class Objective:
    id: str = field(default_factory=lambda: str(uuid4()))
    department: str = "Geral"
    name: str = ""
    krs: List[KeyResult] = field(default_factory=list)
    # Correção do Bug: Estado de expansão salvo no objeto
    expanded: bool = True 
    _app_callback: Optional[callable] = None

    @property
    def progress_avg(self):
        if not self.krs: return 0.0
        return sum(k.progress_pct for k in self.krs) / len(self.krs)

    def add_kr(self, kr: KeyResult):
        kr._callback = self.notify
        self.krs.append(kr)
        self.expanded = True
        self.notify()

    def notify(self):
        if self._app_callback: self._app_callback()

# --- 4. STATE MANAGER ---
class SessionState:
    def __init__(self, user_data):
        self.user = user_data
        self.objectives: List[Objective] = []
        self._dirty = False
        self.load_from_db()

    def mark_dirty(self):
        self._dirty = True
        if 'save_btn' in globals() and save_btn: 
            save_btn.visible = True
            save_btn.update()

    def load_from_db(self):
        df = db.load_okrs(self.user['cliente'])
        self.objectives = self._df_to_objects(df)
        self._dirty = False

    def save_to_db(self):
        df = self.get_dataframe()
        db.save_okrs(df, self.user['cliente'])
        self._dirty = False
        ui.notify('Dados salvos com sucesso!', type='positive')

    # Nova funcionalidade: Renomear Departamento
    def rename_department(self, old_name, new_name):
        changed = False
        for obj in self.objectives:
            if obj.department == old_name:
                obj.department = new_name
                changed = True
        if changed: self.mark_dirty()

    # Nova funcionalidade: Excluir Departamento
    def delete_department(self, dept_name):
        self.objectives = [o for o in self.objectives if o.department != dept_name]
        self.mark_dirty()

    def _df_to_objects(self, df):
        if df.empty: return []
        objs_map = {}
        df = df.fillna('')
        
        for _, row in df.iterrows():
            key = (row['departamento'], row['objetivo'])
            if key not in objs_map:
                objs_map[key] = Objective(department=row['departamento'], name=row['objetivo'], _app_callback=self.mark_dirty)
            
            obj = objs_map[key]
            if not row['kr']: continue
            
            kr = next((k for k in obj.krs if k.name == row['kr']), None)
            if not kr:
                kr = KeyResult(name=row['kr'], target=float(row['alvo'] or 1.0), current=float(row['avanco'] or 0.0))
                obj.add_kr(kr)
            
            if row['tarefa']:
                t = Task(description=row['tarefa'], status=row['status'], responsible=row['responsavel'], deadline=str(row['prazo']))
                kr.add_task(t)
        return list(objs_map.values())

    def get_dataframe(self):
        data = []
        for o in self.objectives:
            for k in o.krs:
                if not k.tasks:
                    data.append([o.department, o.name, k.name, '', '', '', '', k.current, k.target, self.user['cliente']])
                for t in k.tasks:
                    data.append([o.department, o.name, k.name, t.description, t.status, t.responsible, t.deadline, k.current, k.target, self.user['cliente']])
            if not o.krs:
                data.append([o.department, o.name, '', '', '', '', '', 0.0, 1.0, self.user['cliente']])
        cols = ['departamento', 'objetivo', 'kr', 'tarefa', 'status', 'responsavel', 'prazo', 'avanco', 'alvo', 'cliente']
        return pd.DataFrame(data, columns=cols)

# --- 5. COMPONENTES UI AUXILIARES ---

def create_password_input(label='Senha'):
    """Cria input de senha com toggle de visibilidade"""
    pwd = ui.input(label, password=True).classes('w-full')
    with pwd.add_slot('append'):
        ui.icon('visibility').on('click', lambda: pwd.props(
            'type=text' if 'password' in pwd.props else 'type=password'
        )).classes('cursor-pointer')
    return pwd

def manage_departments_dialog(state: SessionState, refresh_cb):
    """Dialog para criar, editar e excluir departamentos"""
    depts = sorted(list(set(o.department for o in state.objectives)))
    
    with ui.dialog() as dialog, ui.card().classes('w-96'):
        ui.label('Gerenciar Departamentos').classes('text-lg font-bold mb-4')
        
        # Lista de departamentos existentes
        with ui.scroll_area().classes('h-64 border p-2 mb-4'):
            if not depts:
                ui.label('Nenhum departamento criado.')
            
            for dept in depts:
                with ui.row().classes('w-full items-center justify-between mb-2'):
                    d_name = ui.input(value=dept).props('dense').classes('flex-grow mr-2')
                    
                    # Botão Salvar Rename
                    def save_rename(old=dept, new_input=d_name):
                        if new_input.value and new_input.value != old:
                            state.rename_department(old, new_input.value)
                            dialog.close()
                            refresh_cb()
                            ui.notify(f'Renomeado para {new_input.value}')
                    
                    ui.button(icon='save', on_click=save_rename).props('flat dense round color=green')
                    
                    # Botão Excluir
                    def delete_dept(d=dept):
                        state.delete_department(d)
                        dialog.close()
                        refresh_cb()
                        ui.notify(f'Departamento {d} excluído')
                    
                    ui.button(icon='delete', on_click=delete_dept).props('flat dense round color=red')

        ui.label('Criar Novo:').classes('font-bold')
        new_d = ui.input('Nome do Departamento').classes('w-full mb-4')
        
        def create_new():
            if new_d.value:
                # Cria um objetivo placeholder para fundar o departamento
                state.objectives.append(Objective(department=new_d.value, name="Objetivo Inicial", _app_callback=state.mark_dirty))
                state.mark_dirty()
                dialog.close()
                refresh_cb()
                ui.notify('Departamento criado!')

        ui.button('Adicionar Departamento', on_click=create_new).classes('w-full bg-blue-600')

# --- 6. TELAS DO SISTEMA ---

# -> LOGIN
@ui.page('/login')
def login_page():
    def try_login():
        user = db.login(username.value, password.value)
        if user:
            app.storage.user['user_info'] = user
            ui.navigate.to('/')
        else:
            ui.notify('Inválido', type='negative')

    def try_register():
        if not (reg_user.value and reg_pass.value and reg_client.value):
            ui.notify('Preencha tudo', type='warning')
            return
        ok, msg = db.create_user(reg_user.value, reg_pass.value, reg_name.value, reg_client.value)
        if ok:
            ui.notify(msg, type='positive')
            tab_panels.value = 'Login'
        else:
            ui.notify(msg, type='negative')

    with ui.card().classes('absolute-center w-96 p-6 shadow-lg'):
        ui.label('OKR Manager').classes('text-2xl font-bold mb-6 w-full text-center text-blue-600')
        
        with ui.tabs().classes('w-full') as tabs:
            ui.tab('Login')
            ui.tab('Cadastro')
        
        with ui.tab_panels(tabs, value='Login').classes('w-full') as tab_panels:
            with ui.tab_panel('Login'):
                username = ui.input('Usuário').classes('w-full mb-2')
                # Correção: Senha com visualização
                password = create_password_input('Senha')
                ui.button('Entrar', on_click=try_login).classes('w-full mt-6 bg-blue-600')
            
            with ui.tab_panel('Cadastro'):
                reg_user = ui.input('Usuário').classes('w-full')
                reg_pass = create_password_input('Senha')
                reg_name = ui.input('Nome Completo').classes('w-full')
                reg_client = ui.input('Nome da Empresa').classes('w-full')
                ui.button('Criar Conta', on_click=try_register).classes('w-full mt-6 color=green')

# -> RENDERIZADORES DE TAREFAS
def render_task_row(task: Task, kr: KeyResult, state: SessionState, refresh_cb):
    with ui.row().classes('w-full items-center gap-2 p-1 border-b border-gray-100 hover:bg-gray-50'):
        ui.input(value=task.description).bind_value(task, 'description').on('blur', state.mark_dirty).classes('flex-grow').props('dense placeholder="Descrição"')
        
        def update_color(e):
            state.mark_dirty()
            e.sender.classes(remove='text-red-500 text-green-500 text-yellow-500')
            if 'Concluído' in task.status: e.sender.classes('text-green-500')
            elif 'Não Iniciado' in task.status: e.sender.classes('text-red-500')
            else: e.sender.classes('text-yellow-600')

        opts = list(CORES_STATUS.keys())
        ui.select(opts, value=task.status).bind_value(task, 'status').on_value_change(update_color).classes('w-36').props('dense options-dense')
        
        ui.input(value=task.responsible).bind_value(task, 'responsible').on('blur', state.mark_dirty).classes('w-24').props('dense placeholder="Resp."')
        
        with ui.input(value=task.deadline).bind_value(task, 'deadline').on('blur', state.mark_dirty).classes('w-32').props('dense placeholder="Prazo"') as d:
            with d.add_slot('append'):
                ui.icon('calendar_month').on('click', lambda: date_menu.open()).classes('cursor-pointer')
            with ui.menu() as date_menu:
                ui.date().bind_value(d).on_value_change(lambda: (date_menu.close(), state.mark_dirty()))
        
        ui.button(icon='delete', color='red', on_click=lambda: (kr.remove_task(task), refresh_cb())).props('flat dense round')

@ui.refreshable
def render_management_panel(state: SessionState):
    depts = sorted(list(set(o.department for o in state.objectives))) or ["Geral"]
    
    # Barra de Ferramentas (Add Depto)
    with ui.row().classes('w-full items-center justify-end mb-2'):
        ui.button('Gerenciar Departamentos', icon='settings', on_click=lambda: manage_departments_dialog(state, render_management_panel.refresh)).props('flat dense color=gray')

    # Quick Create
    with ui.expansion('Criação Rápida de Objetivo', icon='bolt').classes('w-full bg-blue-50 mb-4 border border-blue-100 rounded'):
        with ui.row().classes('w-full items-end gap-2 p-4'):
            d_in = ui.select(depts, label='Departamento', value=depts[0]).classes('w-48')
            o_in = ui.input('Novo Objetivo').classes('flex-grow')
            def quick_add():
                if o_in.value:
                    state.objectives.append(Objective(department=d_in.value, name=o_in.value, _app_callback=state.mark_dirty))
                    state.mark_dirty()
                    o_in.value = ""
                    render_management_panel.refresh()
            ui.button('Adicionar', on_click=quick_add).props('color=blue')

    with ui.tabs().classes('w-full text-blue-600') as tabs:
        for d in depts: ui.tab(d)

    with ui.tab_panels(tabs, value=depts[0]).classes('w-full bg-transparent'):
        for dept in depts:
            with ui.tab_panel(dept):
                objs = [o for o in state.objectives if o.department == dept]
                if not objs: ui.label("Nenhum objetivo aqui.").classes('italic text-gray-400')
                
                for obj in objs:
                    with ui.card().classes('w-full mb-4 border-l-4 border-blue-500'):
                        with ui.row().classes('w-full items-center justify-between'):
                            ui.input(value=obj.name).bind_value(obj, 'name').on('blur', state.mark_dirty).classes('text-lg font-bold w-1/2').props('dense')
                            with ui.row().classes('items-center'):
                                ui.label().bind_text_from(obj, 'progress_avg', lambda x: f"{x*100:.0f}%").classes('font-bold mr-2 text-blue-600')
                                ui.button(icon='delete', color='red', on_click=lambda o=obj: (state.objectives.remove(o), state.mark_dirty(), render_management_panel.refresh())).props('flat round dense')
                        
                        ui.linear_progress(show_value=False).bind_value_from(obj, 'progress_avg').classes('h-2 mb-2')
                        
                        for kr in obj.krs:
                            # Correção do Bug: bind_value no campo 'expanded'
                            exp = ui.expansion(text=kr.name).classes('w-full bg-slate-50 mb-2 border rounded').bind_text_from(kr, 'name', lambda x: f"KR: {x} ({kr.progress_pct*100:.0f}%)")
                            exp.bind_value(kr, 'expanded') 
                            
                            with exp:
                                with ui.column().classes('w-full p-2 bg-white'):
                                    with ui.row().classes('gap-4 mb-2'):
                                        ui.input("KR").bind_value(kr, 'name').on('blur', state.mark_dirty).classes('flex-grow').props('dense')
                                        ui.number("Atual", step=1).bind_value(kr, 'current').on('blur', state.mark_dirty).classes('w-24').props('dense')
                                        ui.number("Meta", step=1).bind_value(kr, 'target').on('blur', state.mark_dirty).classes('w-24').props('dense')
                                        ui.button(icon='delete', color='red', on_click=lambda k=kr, o=obj: (o.krs.remove(k), state.mark_dirty(), render_management_panel.refresh())).props('flat dense')
                                    
                                    for t in kr.tasks: render_task_row(t, kr, state, render_management_panel.refresh)
                                    ui.button("Nova Tarefa", icon='add', on_click=lambda k=kr: (k.add_task(Task()), render_management_panel.refresh())).props('flat dense size=sm')

                        ui.button("Novo KR", icon='add_circle', on_click=lambda o=obj: (o.add_kr(KeyResult("Novo KR")), render_management_panel.refresh())).props('flat color=blue')

@ui.refreshable
def render_dashboard(state: SessionState):
    df = state.get_dataframe()
    if df.empty:
        ui.label('Sem dados.').classes('text-gray-500 italic')
        return

    df_krs = df[df['kr'] != ''].copy()
    if df_krs.empty: return

    # Lógica de Dash (igual anterior)
    with np.errstate(divide='ignore', invalid='ignore'):
        alvo_safe = df_krs['alvo'].replace(0, 1)
        df_krs['pct'] = np.clip(df_krs['avanco'] / alvo_safe, 0, 1)

    hoje = pd.to_datetime(date.today())
    df_krs['prazo_dt'] = pd.to_datetime(df_krs['prazo'], errors='coerce')
    def classificar(row):
        if row['status'] == 'Concluído': return 'Concluído'
        if pd.isna(row['prazo_dt']): return 'Sem Prazo'
        delta = (row['prazo_dt'] - hoje).days
        if delta < 0: return 'Atrasado'
        if delta <= 7: return 'Urgente (7 dias)'
        if delta <= 30: return 'Atenção (30 dias)'
        return 'No Prazo'
    df_krs['classificacao'] = df_krs.apply(classificar, axis=1)

    with ui.row().classes('w-full justify-center gap-4 mb-4'):
        with ui.card().classes('p-4 items-center'):
            ui.label('Progresso').classes('text-xs text-gray-500')
            ui.label(f"{df_krs['pct'].mean()*100:.0f}%").classes('text-4xl font-bold text-blue-600')

    with ui.row().classes('w-full'):
        fig = px.pie(df_krs, names='status', color='status', color_discrete_map=CORES_STATUS, height=300)
        ui.plotly(fig).classes('w-full md:w-1/2')
        
        df_bar = df_krs.groupby(['departamento', 'status']).size().reset_index(name='count')
        fig2 = px.bar(df_bar, y='departamento', x='count', color='status', orientation='h', color_discrete_map=CORES_STATUS, height=300)
        ui.plotly(fig2).classes('w-full md:w-1/2')

def download_excel(state: SessionState):
    df = state.get_dataframe()
    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False)
    ui.download(output.getvalue(), 'okrs.xlsx')

# -> APP PRINCIPAL
@ui.page('/')
def main_page():
    user_info = app.storage.user.get('user_info')
    if not user_info:
        ui.navigate.to('/login')
        return

    state = SessionState(user_info)

    with ui.header().classes('bg-white text-gray-800 shadow-sm p-4 items-center justify-between'):
        ui.label(f"OKR: {user_info['cliente']}").classes('text-xl font-bold')
        with ui.row().classes('items-center gap-2'):
            ui.label(user_info['name'])
            global save_btn
            save_btn = ui.button('Salvar', on_click=state.save_to_db, icon='save').props('color=green')
            save_btn.bind_visibility_from(state, '_dirty')
            ui.button(icon='logout', on_click=lambda: (app.storage.user.clear(), ui.navigate.to('/login'))).props('flat round')

    with ui.row().classes('w-full max-w-7xl mx-auto p-4 gap-4'):
        with ui.card().classes('w-full md:w-64 h-fit p-0'):
            with ui.column().classes('w-full gap-0'):
                def set_page(name):
                    content.clear()
                    with content:
                        if name == 'Painel': render_management_panel(state)
                        elif name == 'Dash': render_dashboard(state)
                
                ui.button('Painel', icon='edit', on_click=lambda: set_page('Painel')).classes('w-full text-left p-4').props('flat')
                ui.button('Dashboard', icon='analytics', on_click=lambda: set_page('Dash')).classes('w-full text-left p-4').props('flat')
                ui.separator()
                ui.button('Excel', icon='download', on_click=lambda: download_excel(state)).classes('w-full text-left p-4 text-green-600').props('flat')

        content = ui.column().classes('flex-grow')
        with content:
            render_management_panel(state)

if __name__ in {"__main__", "__mp_main__"}:
    ui.run(title="OKR SaaS", host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), storage_secret="TROQUE_ISSO_AGORA", language="pt-BR")
