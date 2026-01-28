import os
import time
from uuid import uuid4
from typing import List, Optional, Dict, Any, Callable
from dataclasses import dataclass, field
from datetime import date, datetime
import pandas as pd
import numpy as np
from sqlalchemy import create_engine, Column, String, Float, text
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from nicegui import ui, app
import plotly.express as px
from io import BytesIO

# --- 1. CONFIGURAÇÃO E DEBUG ---
DATABASE_URL = os.getenv("DATABASE_URL")

print("-" * 50)
if not DATABASE_URL:
    print("⚠️  AVISO: DATABASE_URL não encontrada. Usando SQLite Local.")
    DATABASE_URL = "sqlite:///okr_saas.db"
else:
    print("✅  DATABASE_URL encontrada.")
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
print("-" * 50)

# Branding
BRAND = {
    "primary": "#7371ff", "secondary": "#ff43c0",
    "lime": "#bef533", "lavender": "#dbbfff",
    "dark": "#1e1e1e", "gray_light": "#f3f4f6"
}

STATUS_CONFIG = {
    "Não Iniciado": {"color": "#ef4444", "icon": "radio_button_unchecked"},
    "Em Andamento": {"color": BRAND["primary"], "icon": "sync"},
    "Pausado": {"color": "#f59e0b", "icon": "pause_circle"},
    "Concluído": {"color": BRAND["lime"], "text_color": "black", "icon": "check_circle"}
}

# --- 2. PERSISTÊNCIA ---
Base = declarative_base()

class UserDB(Base):
    __tablename__ = 'users'
    username = Column(String, primary_key=True)
    password = Column(String)
    name = Column(String)
    cliente = Column(String)

class OKRDataDB(Base):
    __tablename__ = 'okr_data'
    id = Column(String, primary_key=True, default=lambda: str(uuid4()))
    cliente = Column(String, index=True)
    departamento = Column(String)
    objetivo = Column(String)
    kr = Column(String)
    tarefa = Column(String)
    status = Column(String)
    responsavel = Column(String)
    prazo = Column(String)
    avanco = Column(Float, default=0.0)
    alvo = Column(Float, default=1.0)

class DatabaseManager:
    def __init__(self, url):
        try:
            self.engine = create_engine(url, pool_pre_ping=True)
            Base.metadata.create_all(self.engine)
            self.SessionLocal = sessionmaker(bind=self.engine)
            print("✅  Banco conectado.")
        except Exception as e:
            print(f"❌  ERRO DE CONEXÃO: {e}")

    def get_session(self) -> Session:
        return self.SessionLocal()

    def login(self, username, password) -> Optional[Dict]:
        try:
            with self.get_session() as session:
                user = session.query(UserDB).filter_by(username=username, password=password).first()
                if user: return {"username": user.username, "name": user.name, "cliente": user.cliente}
                return None
        except: return None

    def create_user(self, username, password, name, client) -> tuple[bool, str]:
        try:
            with self.get_session() as session:
                if session.query(UserDB).filter_by(username=username).first():
                    return False, "Usuário já existe"
                new_user = UserDB(username=username, password=password, name=name, cliente=client)
                session.add(new_user)
                session.commit()
                return True, "Usuário criado com sucesso"
        except Exception as e:
            return False, str(e)

    def load_client_data(self, client: str) -> pd.DataFrame:
        try:
            with self.engine.connect() as conn:
                return pd.read_sql(text("SELECT * FROM okr_data WHERE cliente = :c"), conn, params={'c': client})
        except:
            return pd.DataFrame()

    def sync_data(self, df: pd.DataFrame, client: str):
        try:
            with self.get_session() as session:
                session.query(OKRDataDB).filter_by(cliente=client).delete()
                if not df.empty:
                    df['cliente'] = client 
                    data_dicts = df.to_dict(orient='records')
                    session.bulk_insert_mappings(OKRDataDB, data_dicts)
                session.commit()
                return True
        except Exception as e:
            print(f"Erro ao salvar: {e}")
            return False

db_manager = DatabaseManager(DATABASE_URL)

# --- 3. DOMÍNIO ---

@dataclass
class Task:
    id: str = field(default_factory=lambda: str(uuid4()))
    description: str = ""
    status: str = "Não Iniciado"
    responsible: str = ""
    deadline: Optional[str] = None

@dataclass
class KeyResult:
    id: str = field(default_factory=lambda: str(uuid4()))
    name: str = ""
    target: float = 1.0
    current: float = 0.0
    tasks: List[Task] = field(default_factory=list)
    expanded: bool = False

    @property
    def progress(self) -> float:
        if self.target == 0: return 1.0 if self.current >= 0 else 0.0
        return min(max(self.current / self.target, 0.0), 1.0)

@dataclass
class Objective:
    id: str = field(default_factory=lambda: str(uuid4()))
    department: str = "Geral"
    name: str = ""
    krs: List[KeyResult] = field(default_factory=list)
    expanded: bool = True

    @property
    def progress(self) -> float:
        if not self.krs: return 0.0
        return sum(k.progress for k in self.krs) / len(self.krs)

class OKRState:
    def __init__(self, user_info: Dict):
        self.user = user_info
        self.objectives: List[Objective] = []
        self.is_dirty: bool = False
        self.load()

    def mark_dirty(self):
        self.is_dirty = True

    def load(self):
        df = db_manager.load_client_data(self.user['cliente'])
        self.objectives = self._parse_dataframe(df)
        self.is_dirty = False

    def save(self):
        df = self.to_dataframe()
        if db_manager.sync_data(df, self.user['cliente']):
            self.is_dirty = False
            ui.notify("Dados salvos com segurança no Cloud!", type="positive", color=BRAND['lime'], text_color=BRAND['dark'])
        else:
            ui.notify("Erro de conexão ao salvar.", type="negative")

    def rename_department(self, old_name: str, new_name: str):
        if not new_name: return
        changed = False
        for obj in self.objectives:
            if obj.department == old_name:
                obj.department = new_name
                changed = True
        if changed: self.mark_dirty()

    def delete_department(self, dept_name: str):
        initial_len = len(self.objectives)
        self.objectives = [obj for obj in self.objectives if obj.department != dept_name]
        if len(self.objectives) < initial_len:
            self.mark_dirty()

    def _parse_dataframe(self, df: pd.DataFrame) -> List[Objective]:
        if df.empty: return []
        df = df.fillna('')
        objs_dict = {}
        
        for _, row in df.iterrows():
            obj_key = (row['departamento'], row['objetivo'])
            if obj_key not in objs_dict:
                objs_dict[obj_key] = Objective(department=row['departamento'], name=row['objetivo'])
            
            obj = objs_dict[obj_key]
            if not row['kr']: continue
            
            kr = next((k for k in obj.krs if k.name == row['kr']), None)
            if not kr:
                kr = KeyResult(name=row['kr'], target=float(row['alvo'] or 1.0), current=float(row['avanco'] or 0.0))
                obj.krs.append(kr)
            
            if row['tarefa']:
                task = Task(description=row['tarefa'], status=row['status'], 
                           responsible=row['responsavel'], deadline=str(row['prazo']))
                kr.tasks.append(task)
        
        return list(objs_dict.values())

    def to_dataframe(self) -> pd.DataFrame:
        rows = []
        client = self.user['cliente']
        for obj in self.objectives:
            if not obj.krs:
                rows.append([obj.department, obj.name, "", "", "", "", "", 0.0, 1.0, client])
                continue
            for kr in obj.krs:
                if not kr.tasks:
                    rows.append([obj.department, obj.name, kr.name, "", "", "", "", kr.current, kr.target, client])
                    continue
                for task in kr.tasks:
                    rows.append([obj.department, obj.name, kr.name, task.description, task.status, 
                                task.responsible, task.deadline, kr.current, kr.target, client])
        
        cols = ['departamento', 'objetivo', 'kr', 'tarefa', 'status', 'responsavel', 'prazo', 
                'avanco', 'alvo', 'cliente']
        return pd.DataFrame(rows, columns=cols)

    def add_objective(self, department: str, name: str):
        self.objectives.append(Objective(department=department, name=name))
        self.mark_dirty()

    def remove_objective(self, obj: Objective):
        self.objectives.remove(obj)
        self.mark_dirty()

    def get_departments(self) -> List[str]:
        depts = sorted(list(set(o.department for o in self.objectives)))
        return depts if depts else ["Geral"]

# --- 4. COMPONENTES UI ---

class UIComponents:
    @staticmethod
    def section_title(title: str, icon: str = None):
        with ui.row().classes('items-center gap-2 mb-4'):
            if icon: ui.icon(icon).classes('text-2xl').props(f'color={BRAND["primary"]}')
            ui.label(title).classes('text-xl font-bold').style(f'color: {BRAND["dark"]}')

    @staticmethod
    def card_container():
        return ui.card().classes('w-full shadow-sm border border-slate-200 rounded-lg p-4 bg-white')

# --- 5. VIEWS ---

@ui.page('/login')
def login_page():
    if app.storage.user.get('authenticated'):
        ui.navigate.to('/')
        return

    async def handle_login():
        user = db_manager.login(username.value, password.value)
        if user:
            app.storage.user.update({'authenticated': True, 'user_info': user})
            ui.navigate.to('/')
        else:
            ui.notify("Login falhou", type="negative")

    async def handle_register():
        if not all([reg_user.value, reg_pass.value, reg_name.value, reg_client.value]):
            ui.notify("Preencha tudo", type="warning")
            return
        success, msg = db_manager.create_user(reg_user.value, reg_pass.value, reg_name.value, reg_client.value)
        if success:
            ui.notify(msg, type="positive", color=BRAND['lime'], text_color='black')
            tabs.value = 'Login'
        else:
            ui.notify(msg, type="negative")

    with ui.column().classes('absolute-center w-full max-w-md p-4'):
        with ui.card().classes('w-full shadow-2xl p-8 border-t-4').style(f'border-top-color: {BRAND["primary"]}'):
            ui.label('OKR SaaS').classes('text-3xl font-black text-center mb-2').style(f'color: {BRAND["primary"]}')
            
            with ui.tabs().classes('w-full').props(f'active-color={BRAND["secondary"]} indicator-color={BRAND["secondary"]}') as tabs:
                ui.tab('Login', icon='login')
                ui.tab('Cadastro', icon='person_add')
            
            with ui.tab_panels(tabs, value='Login').classes('w-full mt-4'):
                with ui.tab_panel('Login'):
                    username = ui.input('Usuário').classes('w-full').props('outlined dense')
                    password = ui.input('Senha', password=True).classes('w-full mt-4').props('outlined dense')
                    with password.add_slot('append'):
                        ui.icon('visibility').on('click', lambda: password.props(
                            'type=text' if 'password' in password.props else 'type=password'
                        )).classes('cursor-pointer')
                    ui.button('Entrar', on_click=handle_login).classes('w-full mt-8 font-bold text-white').style(f'background-color: {BRAND["primary"]}')
                
                with ui.tab_panel('Cadastro'):
                    reg_name = ui.input('Nome').classes('w-full').props('outlined dense')
                    reg_client = ui.input('Empresa').classes('w-full mt-2').props('outlined dense')
                    reg_user = ui.input('Usuário').classes('w-full mt-2').props('outlined dense')
                    reg_pass = ui.input('Senha', password=True).classes('w-full mt-2').props('outlined dense')
                    with reg_pass.add_slot('append'):
                        ui.icon('visibility').on('click', lambda: reg_pass.props(
                            'type=text' if 'password' in reg_pass.props else 'type=password'
                        )).classes('cursor-pointer')
                    ui.button('Criar Conta', on_click=handle_register).classes('w-full mt-8 font-bold text-black').style(f'background-color: {BRAND["lime"]}')

@ui.refreshable
def render_management(state: OKRState):
    depts = state.get_departments()
    
    with ui.row().classes('w-full justify-between items-center mb-6'):
        UIComponents.section_title("Gestão Estratégica", "assignment")
        with ui.row().classes('gap-2'):
            ui.button('Novo Objetivo', icon='add', on_click=lambda: add_obj_dialog.open()).props('rounded elevated').style(f'background-color: {BRAND["primary"]}; color: white')
            ui.button('Departamentos', icon='edit', on_click=lambda: dept_dialog.open()).props('flat').style(f'color: {BRAND["secondary"]}')

    with ui.dialog() as add_obj_dialog, ui.card().classes('w-96'):
        ui.label('Novo Objetivo').classes('text-lg font-bold mb-4').style(f'color: {BRAND["dark"]}')
        d_sel = ui.select(depts, label="Departamento", value=depts[0]).classes('w-full')
        o_name = ui.input("Título").classes('w-full')
        with ui.row().classes('w-full justify-end mt-4'):
            ui.button('Cancelar', on_click=add_obj_dialog.close).props('flat')
            def confirm_add():
                if o_name.value:
                    state.add_objective(d_sel.value, o_name.value)
                    add_obj_dialog.close()
                    render_management.refresh()
            ui.button('Criar', on_click=confirm_add).props('elevated').style(f'background-color: {BRAND["primary"]}; color: white')

    with ui.dialog() as dept_dialog, ui.card().classes('w-[500px] h-[400px] p-0'):
        with ui.column().classes('w-full h-full'):
            ui.label('Gerenciar Departamentos').classes('text-lg font-bold p-4 border-b w-full')
            with ui.scroll_area().classes('flex-grow w-full p-4'):
                if not depts: ui.label('Vazio').classes('text-slate-400')
                for d in depts:
                    with ui.row().classes('w-full items-center justify-between mb-2 group hover:bg-gray-50 p-2 rounded'):
                        d_input = ui.input(value=d).props('dense borderless').classes('font-medium flex-grow')
                        def handle_rename(new_val, old_val=d):
                            if new_val and new_val != old_val:
                                state.rename_department(old_val, new_val)
                                dept_dialog.close()
                                render_management.refresh()
                        d_input.on('blur', lambda e, i=d_input: handle_rename(i.value))
                        ui.button(icon='delete', on_click=lambda d=d: (state.delete_department(d), dept_dialog.close(), render_management.refresh())).props('flat dense round color=red')
            with ui.row().classes('w-full p-4 border-t gap-2 items-center bg-gray-50'):
                new_d_input = ui.input(placeholder='Novo...').classes('flex-grow').props('outlined dense bg-white')
                def create_d():
                    if new_d_input.value:
                        state.add_objective(new_d_input.value, "Objetivo Inicial")
                        dept_dialog.close()
                        render_management.refresh()
                ui.button('Adicionar', icon='add', on_click=create_d).style(f'background-color: {BRAND["primary"]}; color: white')

    with ui.tabs().classes('w-full border-b border-slate-200').props(f'active-color={BRAND["primary"]} indicator-color={BRAND["primary"]}') as tabs:
        for d in depts: ui.tab(d)

    with ui.tab_panels(tabs, value=depts[0]).classes('w-full bg-transparent mt-4'):
        for dept in depts:
            with ui.tab_panel(dept):
                objs = [o for o in state.objectives if o.department == dept]
                if not objs: ui.label("Sem objetivos.").classes('text-slate-400 italic py-8 text-center w-full')
                
                for obj in objs:
                    with UIComponents.card_container().classes('mb-6'):
                        # Objetivo
                        with ui.row().classes('w-full items-center gap-4'):
                            ui.input().bind_value(obj, 'name').on('blur', state.mark_dirty).classes('text-lg font-bold flex-grow').props('borderless dense')
                            
                            # Knob Progresso
                            ui.knob(obj.progress, show_value=False, size='32px', track_color='grey-3').props(f'readonly color={BRAND["primary"]}')
                            ui.label().bind_text_from(obj, 'progress', lambda p: f"{p*100:.0f}%").classes('font-black text-xl').style(f'color: {BRAND["primary"]}')
                            
                            with ui.button(icon='more_vert').props('flat round'):
                                with ui.menu():
                                    with ui.menu_item(on_click=lambda o=obj: (state.remove_objective(o), render_management.refresh())):
                                        ui.label('Excluir').classes('text-red-500')
                        
                        # Lista KRs
                        with ui.column().classes('w-full mt-4 gap-2'):
                            for kr in obj.krs:
                                with ui.expansion().classes('w-full border border-slate-100 rounded').style(f'background-color: {BRAND["gray_light"]}') as exp:
                                    exp.bind_value(kr, 'expanded')
                                    with exp.add_slot('header'):
                                        with ui.row().classes('w-full items-center'):
                                            ui.label(f"KR: {kr.name}").classes('font-medium flex-grow')
                                            ui.label(f"{kr.current}/{kr.target}").classes('text-xs text-slate-500 mr-4')
                                            ui.knob(kr.progress, show_value=False, size='24px', track_color='grey-3').props(f'readonly color={BRAND["secondary"]}')
                                    
                                    with ui.column().classes('w-full p-4 bg-white gap-4'):
                                        # Métricas KR
                                        with ui.row().classes('w-full gap-4 items-center'):
                                            ui.input('Descrição do KR').bind_value(kr, 'name').on('blur', state.mark_dirty).classes('flex-grow').props('outlined dense')
                                            ui.number('Atual').bind_value(kr, 'current').on('blur', state.mark_dirty).on('change', lambda: render_management.refresh()).classes('w-24').props('outlined dense')
                                            ui.number('Meta').bind_value(kr, 'target').on('blur', state.mark_dirty).on('change', lambda: render_management.refresh()).classes('w-24').props('outlined dense')
                                            ui.button(icon='delete', on_click=lambda k=kr, o=obj: (o.krs.remove(k), state.mark_dirty(), render_management.refresh())).props('flat round color=red')
                                    
                                        # Tarefas
                                        ui.separator()
                                        ui.label('Plano de Ação').classes('text-xs font-bold text-slate-400 uppercase tracking-widest')
                                        for task in kr.tasks:
                                            with ui.row().classes('w-full items-center gap-2 p-2 rounded border border-slate-100').style(f'background-color: {BRAND["gray_light"]}'):
                                                # Descrição
                                                ui.input().bind_value(task, 'description').on('blur', state.mark_dirty).classes('flex-grow').props('borderless dense placeholder="O que fazer?"')
                                                
                                                def get_status_props(s):
                                                    conf = STATUS_CONFIG.get(s, STATUS_CONFIG["Não Iniciado"])
                                                    txt_color = conf.get("text_color", conf["color"])
                                                    return f'color={txt_color}'

                                                s_sel = ui.select(list(STATUS_CONFIG.keys()), value=task.status).bind_value(task, 'status').on_value_change(state.mark_dirty)
                                                s_sel.classes('w-36').props('borderless dense options-dense')
                                                
                                                # Responsável
                                                ui.input().bind_value(task, 'responsible').on('blur', state.mark_dirty).classes('w-24').props('borderless dense placeholder="Resp."')

                                                # Prazo
                                                with ui.input().bind_value(task, 'deadline').on('blur', state.mark_dirty).classes('w-28').props('borderless dense placeholder="Prazo"') as d:
                                                    with d.add_slot('append'):
                                                        ui.icon('calendar_today').on('click', lambda: date_menu.open()).classes('cursor-pointer text-xs text-slate-400')
                                                    with ui.menu() as date_menu:
                                                        ui.date().bind_value(d).on_value_change(lambda: (date_menu.close(), state.mark_dirty()))
                                                
                                                ui.button(icon='close', on_click=lambda t=task, k=kr: (k.tasks.remove(t), state.mark_dirty(), render_management.refresh())).props('flat round dense size=sm color=red')
                                    
                                        ui.button('Nova Tarefa', icon='add', on_click=lambda k=kr: (k.tasks.append(Task()), render_management.refresh())).props('flat size=sm width=full').style(f'color: {BRAND["primary"]}')

                            ui.button('Novo Key Result', icon='add_circle_outline', on_click=lambda o=obj: (o.krs.append(KeyResult(name="Novo KR")), render_management.refresh())).props('flat classes="mt-2"').style(f'color: {BRAND["secondary"]}')

@ui.refreshable
def render_dashboard(state: OKRState):
    df = state.to_dataframe()
    if df.empty or (len(df) == 1 and df['kr'].iloc[0] == ""):
        ui.label('Sem dados.').classes('text-slate-400 mt-4')
        return

    UIComponents.section_title("Dashboard Executivo", "insights")
    df_krs = df[df['kr'] != ''].copy()
    df_krs['pct'] = np.clip(df_krs['avanco'] / df_krs['alvo'].replace(0, 1), 0, 1)
    
    with ui.row().classes('w-full gap-4 mb-8'):
        def kpi_card(title, value, color):
            with ui.card().classes('flex-grow p-6 items-center border-b-4').style(f'border-bottom-color: {color}'):
                ui.label(title).classes('text-xs font-bold text-slate-400 uppercase')
                ui.label(value).classes('text-4xl font-black text-slate-800')

        kpi_card('Progresso Global', f"{df_krs['pct'].mean()*100:.1f}%", BRAND['primary'])
        kpi_card('KRs Concluídos', f"{len(df_krs[df_krs['pct'] >= 1])}", BRAND['lime'])
        kpi_card('Ações Totais', str(len(df)), BRAND['secondary'])

    with ui.row().classes('w-full gap-4'):
        with ui.card().classes('flex-grow p-4 h-96'):
            ui.label('Status').classes('font-bold mb-4')
            fig = px.pie(df_krs, names='status', color='status', color_discrete_map={k: v['color'] for k, v in STATUS_CONFIG.items()})
            ui.plotly(fig).classes('w-full h-full')
            
        with ui.card().classes('flex-grow p-4 h-96'):
            ui.label('Por Departamento').classes('font-bold mb-4')
            df_dept = df_krs.groupby('departamento')['pct'].mean().reset_index()
            fig2 = px.bar(df_dept, x='pct', y='departamento', orientation='h', color_discrete_sequence=[BRAND['primary']])
            ui.plotly(fig2).classes('w-full h-full')

def export_excel(state: OKRState):
    df = state.to_dataframe()
    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False)
    ui.download(output.getvalue(), f'OKRs.xlsx')

# --- 6. APP LAYOUT ---

@ui.page('/')
def main_page():
    user_info = app.storage.user.get('user_info')
    if not user_info:
        ui.navigate.to('/login')
        return

    state = OKRState(user_info)

    ui.colors(primary=BRAND['primary'], secondary=BRAND['secondary'], accent=BRAND['lime'], positive=BRAND['lime'])

    with ui.header().classes('bg-white border-b border-slate-200 text-slate-800 p-4 justify-between items-center'):
        with ui.row().classes('items-center gap-4'):
            ui.button(icon='menu', on_click=lambda: drawer.toggle()).props('flat round color=slate')
            ui.label('OKR Manager').classes('text-xl font-black').style(f'color: {BRAND["primary"]}')
            ui.badge(user_info['cliente']).style(f'background-color: {BRAND["lavender"]}; color: {BRAND["dark"]}')
        
        with ui.row().classes('items-center gap-4'):
            save_btn = ui.button('Salvar', icon='save', on_click=state.save)
            save_btn.style(f'background-color: {BRAND["lime"]}; color: black; font-weight: bold;')
            save_btn.bind_visibility_from(state, 'is_dirty')
            
            with ui.avatar(text_color='white').style(f'background-color: {BRAND["primary"]}'):
                ui.label(user_info['name'][0].upper())
            
            with ui.button(icon='expand_more').props('flat round'):
                with ui.menu():
                    with ui.menu_item(on_click=lambda: (app.storage.user.clear(), ui.navigate.to('/login'))):
                        with ui.row().classes('items-center gap-2'):
                            ui.icon('logout')
                            ui.label('Sair')

    with ui.left_drawer().classes('bg-slate-50 border-r border-slate-200 p-0') as drawer:
        with ui.column().classes('w-full gap-0'):
            def navigate_to(view_func):
                content.clear()
                with content: view_func(state)
                if ui.query('body').classes('w-full').width < 1024: 
                    drawer.close()

            ui.label('MENU').classes('text-[10px] font-bold text-slate-400 px-6 py-4 tracking-widest')
            ui.button('Gestão', icon='dashboard', on_click=lambda: navigate_to(render_management)).classes('w-full justify-start px-6 py-4 h-auto text-slate-600').props('flat no-caps')
            ui.button('Dashboard', icon='insights', on_click=lambda: navigate_to(render_dashboard)).classes('w-full justify-start px-6 py-4 h-auto text-slate-600').props('flat no-caps')
            
            ui.separator().classes('my-2')
            ui.button('Excel', icon='file_download', on_click=lambda: export_excel(state)).classes('w-full justify-start px-6 py-4 h-auto').style(f'color: {BRAND["primary"]}').props('flat no-caps')

    content = ui.column().classes('w-full max-w-6xl mx-auto p-6 flex-grow')
    with content:
        render_management(state)

# --- 7. INICIALIZAÇÃO ---
if __name__ in {"__main__", "__mp_main__"}:
    ui.run(
        title="OKR SaaS",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8080)),
        storage_secret=os.getenv("STORAGE_SECRET", "super-secret-key-123"),
        language="pt-BR",
        favicon="🎯"
    )
