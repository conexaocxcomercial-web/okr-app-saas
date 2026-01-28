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

# Paleta simplificada e profissional
BRAND = {
    "primary": "#4f46e5",  # Indigo 600
    "secondary": "#8b5cf6",  # Violet 500
    "accent": "#10b981",  # Emerald 500
    "dark": "#0f172a",  # Slate 900
    "text": "#1e293b",  # Slate 800
    "text_light": "#64748b",  # Slate 500
    "bg": "#ffffff",
    "bg_subtle": "#f8fafc",  # Slate 50
    "border": "#e2e8f0",  # Slate 200
    "success": "#10b981",
    "warning": "#f59e0b",
    "error": "#ef4444"
}

# Status simplificado e consistente
STATUS_CONFIG = {
    "Não Iniciado": {
        "color": "#ef4444",
        "icon": "radio_button_unchecked",
        "bg": "#fef2f2"
    },
    "Em Andamento": {
        "color": "#3b82f6",
        "icon": "pending",
        "bg": "#eff6ff"
    },
    "Pausado": {
        "color": "#f59e0b",
        "icon": "pause_circle_outline",
        "bg": "#fffbeb"
    },
    "Concluído": {
        "color": "#10b981",
        "icon": "check_circle",
        "bg": "#f0fdf4"
    }
}

# --- 2. PERSISTÊNCIA (ORM) ---
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
        self.SessionLocal = None
        self.init_error = None
        
        if not url:
            self.init_error = "Variável DATABASE_URL não encontrada no Render."
            print(f"❌ {self.init_error}")
            return

        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)

        try:
            self.engine = create_engine(
                url,
                pool_pre_ping=True,
                pool_recycle=1800,
                connect_args={
                    "connect_timeout": 10,
                    "keepalives": 1,
                }
            )
            Base.metadata.create_all(self.engine)
            self.SessionLocal = sessionmaker(bind=self.engine)
            print("✅ Banco conectado com sucesso via Pooler!")
            
        except Exception as e:
            self.init_error = str(e)
            print(f"❌ ERRO CRÍTICO DE CONEXÃO: {e}")

    def get_session(self) -> Session:
        if self.SessionLocal is None:
            raise Exception(f"Banco desconectado: {self.init_error}")
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
            if self.SessionLocal is None: return pd.DataFrame()
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
            ui.notify("Alterações salvas", type="positive", color=BRAND['success'], icon="check_circle", position="top")
        else:
            err = db_manager.init_error or "Erro de conexão"
            ui.notify(f"Falha ao salvar: {err}", type="negative", position="top")

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
    def section_title(title: str, subtitle: str = None, icon: str = None):
        """Header limpo e direto"""
        with ui.column().classes('gap-2 mb-8'):
            with ui.row().classes('items-center gap-3'):
                if icon: 
                    ui.icon(icon, size='md').style(f'color: {BRAND["primary"]}')
                ui.label(title).classes('text-2xl font-bold').style(f'color: {BRAND["text"]}')
            if subtitle:
                ui.label(subtitle).classes('text-sm').style(f'color: {BRAND["text_light"]}')

    @staticmethod
    def empty_state(icon: str, title: str, message: str, action_label: str = None, action_callback = None):
        """Empty state simplificado"""
        with ui.column().classes('items-center justify-center py-16 w-full'):
            ui.icon(icon, size='3xl').classes('opacity-20').style(f'color: {BRAND["text_light"]}')
            ui.label(title).classes('text-xl font-semibold mt-6').style(f'color: {BRAND["text"]}')
            ui.label(message).classes('text-sm text-center max-w-md mt-2').style(f'color: {BRAND["text_light"]}')
            if action_label and action_callback:
                ui.button(action_label, icon='add', on_click=action_callback).classes('mt-6').style(
                    f'background-color: {BRAND["primary"]}; color: white; font-weight: 600;'
                ).props('no-caps unelevated')

    @staticmethod
    def card_container(elevated: bool = False):
        """Card limpo e consistente - sombra reduzida"""
        classes = 'w-full rounded-xl p-6 bg-white'
        if elevated:
            classes += ' shadow-sm hover:shadow-md transition-shadow'
        else:
            classes += ' border'
        return ui.card().classes(classes).style(f'border-color: {BRAND["border"]}')

    @staticmethod
    def progress_indicator(progress: float, size: str = 'md', show_label: bool = True):
        """Indicador simplificado"""
        sizes = {'sm': '40px', 'md': '56px', 'lg': '64px'}
        
        # Cores diretas sem gradações complexas
        if progress >= 0.8:
            color = BRAND['success']
        elif progress >= 0.5:
            color = BRAND['warning']
        else:
            color = BRAND['error']
        
        with ui.row().classes('items-center gap-2'):
            ui.circular_progress(value=progress, size=sizes[size], color=color, show_value=False).props('thickness=0.15')
            if show_label:
                ui.label(f"{progress*100:.0f}%").classes('text-base font-semibold').style(f'color: {color}')

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
            ui.notify("Usuário ou senha incorretos", type="negative", position="top")

    async def handle_register():
        if not all([reg_user.value, reg_pass.value, reg_name.value, reg_client.value]):
            ui.notify("Preencha todos os campos", type="warning", position="top")
            return
        
        success, msg = db_manager.create_user(reg_user.value, reg_pass.value, reg_name.value, reg_client.value)
        
        if success:
            ui.notify(msg, type="positive", color=BRAND['success'], position="top")
            tabs.value = 'Login'
        else:
            ui.notify(f"Erro: {msg}", type="negative", position="top")

    def toggle_password_visibility(input_field):
        current_type = input_field._props.get('type', 'password')
        if current_type == 'password':
            input_field.props('type=text')
        else:
            input_field.props('type=password')

    # Login limpo sem gradientes excessivos
    with ui.column().classes('absolute-center w-full max-w-md px-6'):
        with ui.card().classes('w-full shadow-lg rounded-xl overflow-hidden'):
            with ui.column().classes('w-full p-8 items-center bg-white'):
                ui.label('OKR Manager').classes('text-3xl font-black').style(f'color: {BRAND["primary"]}')
                ui.label('Gestão estratégica de objetivos').classes('text-sm mt-1').style(f'color: {BRAND["text_light"]}')
            
            with ui.column().classes('p-8'):
                with ui.tabs().classes('w-full').props(f'active-color={BRAND["primary"]} indicator-color={BRAND["primary"]}') as tabs:
                    ui.tab('Login', icon='login')
                    ui.tab('Cadastro', icon='person_add')
                
                with ui.tab_panels(tabs, value='Login').classes('w-full mt-6'):
                    with ui.tab_panel('Login'):
                        with ui.column().classes('w-full gap-4'):
                            username = ui.input('E-mail', placeholder='seu@email.com').classes('w-full').props('outlined')
                            password = ui.input('Senha', password=True, placeholder='••••••••').classes('w-full').props('outlined type=password')
                            with password.add_slot('append'):
                                ui.icon('visibility').on('click', lambda: toggle_password_visibility(password)).classes('cursor-pointer')
                            ui.button('Entrar', on_click=handle_login, icon='login').classes('w-full mt-2').style(
                                f'background-color: {BRAND["primary"]}; color: white; font-weight: 600;'
                            ).props('no-caps unelevated')
                    
                    with ui.tab_panel('Cadastro'):
                        with ui.column().classes('w-full gap-4'):
                            reg_name = ui.input('Nome completo', placeholder='João Silva').classes('w-full').props('outlined')
                            reg_client = ui.input('Empresa', placeholder='Nome da sua empresa').classes('w-full').props('outlined')
                            reg_user = ui.input('E-mail', placeholder='seu@email.com').classes('w-full').props('outlined')
                            reg_pass = ui.input('Senha', password=True, placeholder='Mínimo 8 caracteres').classes('w-full').props('outlined type=password')
                            with reg_pass.add_slot('append'):
                                ui.icon('visibility').on('click', lambda: toggle_password_visibility(reg_pass)).classes('cursor-pointer')
                            ui.button('Criar conta', on_click=handle_register, icon='person_add').classes('w-full mt-2').style(
                                f'background-color: {BRAND["success"]}; color: white; font-weight: 600;'
                            ).props('no-caps unelevated')

@ui.refreshable
def render_management(state: OKRState):
    depts = state.get_departments()
    
    # Header simplificado
    with ui.row().classes('w-full justify-between items-center mb-8'):
        UIComponents.section_title(
            "Objetivos Estratégicos", 
            "Gerencie seus OKRs e acompanhe o progresso",
            "flag"
        )
        with ui.row().classes('gap-2'):
            ui.button('Departamentos', icon='corporate_fare', on_click=lambda: dept_dialog.open()).props('outline').style(
                f'color: {BRAND["text_light"]}; border-color: {BRAND["border"]}'
            )
            ui.button('Novo Objetivo', icon='add', on_click=lambda: add_obj_dialog.open()).style(
                f'background-color: {BRAND["primary"]}; color: white; font-weight: 600;'
            ).props('no-caps unelevated')

    # Dialog novo objetivo
    with ui.dialog() as add_obj_dialog, ui.card().classes('w-[500px] p-0 rounded-xl shadow-lg'):
        with ui.column().classes('w-full'):
            with ui.row().classes('w-full p-6 items-center justify-between border-b').style(
                f'border-color: {BRAND["border"]}'
            ):
                ui.label('Novo objetivo').classes('text-lg font-bold').style(f'color: {BRAND["text"]}')
                ui.button(icon='close', on_click=add_obj_dialog.close).props('flat round dense')
            
            with ui.column().classes('p-6 gap-4'):
                d_sel = ui.select(depts, label="Departamento", value=depts[0]).classes('w-full').props('outlined')
                o_name = ui.input("Nome do objetivo", placeholder="Ex: Aumentar satisfação dos clientes").classes('w-full').props('outlined')
                
                with ui.row().classes('w-full justify-end gap-2 mt-4'):
                    ui.button('Cancelar', on_click=add_obj_dialog.close).props('flat').style(
                        f'color: {BRAND["text_light"]}'
                    )
                    def confirm_add():
                        if o_name.value:
                            state.add_objective(d_sel.value, o_name.value)
                            add_obj_dialog.close()
                            render_management.refresh()
                            ui.notify("Objetivo criado", type="positive", color=BRAND['success'], position="top")
                    ui.button('Criar', icon='add', on_click=confirm_add).style(
                        f'background-color: {BRAND["primary"]}; color: white; font-weight: 600;'
                    ).props('no-caps unelevated')

    # Dialog departamentos
    with ui.dialog() as dept_dialog, ui.card().classes('w-[560px] h-[520px] p-0 rounded-xl shadow-lg'):
        with ui.column().classes('w-full h-full'):
            with ui.row().classes('w-full p-6 items-center justify-between border-b').style(
                f'border-color: {BRAND["border"]}'
            ):
                ui.label('Gerenciar departamentos').classes('text-lg font-bold').style(f'color: {BRAND["text"]}')
                ui.button(icon='close', on_click=dept_dialog.close).props('flat round dense')
            
            with ui.scroll_area().classes('flex-grow w-full p-6'):
                if not depts: 
                    UIComponents.empty_state(
                        'corporate_fare',
                        'Nenhum departamento',
                        'Departamentos são criados automaticamente'
                    )
                else:
                    with ui.column().classes('w-full gap-2'):
                        for d in depts:
                            with ui.card().classes('w-full p-4 border rounded-lg').style(
                                f'border-color: {BRAND["border"]}'
                            ):
                                with ui.row().classes('w-full items-center gap-3'):
                                    ui.icon('folder', size='sm').style(f'color: {BRAND["primary"]}')
                                    d_input = ui.input(value=d).props('borderless').classes('font-medium flex-grow').style(
                                        f'color: {BRAND["text"]}'
                                    )
                                    def handle_rename(new_val, old_val=d):
                                        if new_val and new_val != old_val:
                                            state.rename_department(old_val, new_val)
                                            dept_dialog.close()
                                            render_management.refresh()
                                            ui.notify("Departamento renomeado", type="positive", color=BRAND['success'], position="top")
                                    d_input.on('blur', lambda e, i=d_input: handle_rename(i.value))
                                    ui.button(icon='delete_outline', on_click=lambda d=d: (
                                        state.delete_department(d), 
                                        dept_dialog.close(), 
                                        render_management.refresh(),
                                        ui.notify("Departamento excluído", type="info", position="top")
                                    )).props('flat dense round').style(f'color: {BRAND["error"]}')
            
            with ui.row().classes('w-full p-6 border-t gap-2 items-center').style(
                f'border-color: {BRAND["border"]}'
            ):
                new_d_input = ui.input(placeholder='Novo departamento').classes('flex-grow').props('outlined dense')
                def create_d():
                    if new_d_input.value:
                        state.add_objective(new_d_input.value, "Objetivo Inicial")
                        dept_dialog.close()
                        render_management.refresh()
                        ui.notify("Departamento criado", type="positive", color=BRAND['success'], position="top")
                ui.button('Adicionar', icon='add', on_click=create_d).style(
                    f'background-color: {BRAND["primary"]}; color: white; font-weight: 600;'
                ).props('no-caps unelevated')

    # Tabs simplificadas
    with ui.tabs().classes('w-full mb-6').props(
        f'active-color={BRAND["primary"]} indicator-color={BRAND["primary"]} dense'
    ) as tabs:
        for d in depts: 
            ui.tab(d, icon='folder')

    # Conteúdo dos departamentos
    with ui.tab_panels(tabs, value=depts[0]).classes('w-full bg-transparent'):
        for dept in depts:
            with ui.tab_panel(dept).classes('p-0'):
                objs = [o for o in state.objectives if o.department == dept]
                
                if not objs: 
                    UIComponents.empty_state(
                        'track_changes',
                        f'Nenhum objetivo em {dept}',
                        'Crie seu primeiro objetivo estratégico',
                        'Criar objetivo',
                        lambda: add_obj_dialog.open()
                    )
                else:
                    with ui.column().classes('w-full gap-6'):
                        for obj in objs:
                            # Card do objetivo - hierarquia clara sem excessos
                            with UIComponents.card_container(elevated=True):
                                # Header do objetivo - NÍVEL 1
                                with ui.row().classes('w-full items-start gap-4 pb-5 border-b').style(
                                    f'border-color: {BRAND["border"]}'
                                ):
                                    with ui.column().classes('flex-grow gap-2'):
                                        with ui.row().classes('items-center gap-2'):
                                            ui.icon('flag', size='sm').style(f'color: {BRAND["primary"]}')
                                            ui.input().bind_value(obj, 'name').on('blur', state.mark_dirty).classes(
                                                'text-xl font-bold flex-grow'
                                            ).props('borderless dense').style(f'color: {BRAND["text"]}')
                                        with ui.row().classes('items-center gap-3 ml-7'):
                                            ui.label(f'{len(obj.krs)} KRs').classes('text-xs px-2 py-1 rounded').style(
                                                f'background-color: {BRAND["bg_subtle"]}; color: {BRAND["text_light"]}'
                                            )
                                            ui.label(f'{sum(len(kr.tasks) for kr in obj.krs)} tarefas').classes('text-xs px-2 py-1 rounded').style(
                                                f'background-color: {BRAND["bg_subtle"]}; color: {BRAND["text_light"]}'
                                            )
                                    
                                    with ui.column().classes('items-end gap-1'):
                                        UIComponents.progress_indicator(obj.progress, 'md', True)
                                    
                                    with ui.button(icon='more_vert', on_click=None).props('flat round dense'):
                                        with ui.menu():
                                            with ui.menu_item(on_click=lambda o=obj: (
                                                state.remove_objective(o), 
                                                render_management.refresh(),
                                                ui.notify("Objetivo excluído", type="info", position="top")
                                            )):
                                                with ui.row().classes('items-center gap-2'):
                                                    ui.icon('delete_outline', size='sm').style(f'color: {BRAND["error"]}')
                                                    ui.label('Excluir').style(f'color: {BRAND["error"]}')
                                
                                # Key Results - NÍVEL 2
                                if not obj.krs:
                                    with ui.column().classes('w-full items-center py-10'):
                                        ui.icon('analytics', size='lg').classes('opacity-20').style(f'color: {BRAND["text_light"]}')
                                        ui.label('Nenhum Key Result').classes('text-sm font-medium mt-3').style(f'color: {BRAND["text"]}')
                                        ui.button('Adicionar Key Result', icon='add_circle_outline', on_click=lambda o=obj: (
                                            o.krs.append(KeyResult(name="Novo Key Result")), 
                                            render_management.refresh()
                                        )).props('flat').classes('mt-3').style(f'color: {BRAND["primary"]}')
                                else:
                                    with ui.column().classes('w-full mt-5 gap-3'):
                                        for kr in obj.krs:
                                            with ui.expansion().classes('w-full rounded-lg overflow-hidden border').style(
                                                f'background-color: {BRAND["bg_subtle"]}; border-color: {BRAND["border"]}'
                                            ) as exp:
                                                exp.bind_value(kr, 'expanded')
                                                
                                                # Header do KR
                                                with exp.add_slot('header'):
                                                    with ui.row().classes('w-full items-center gap-3 px-2'):
                                                        ui.icon('show_chart', size='sm').style(f'color: {BRAND["secondary"]}')
                                                        ui.label().bind_text_from(kr, 'name', lambda n: n or 'Sem nome').classes(
                                                            'font-semibold flex-grow'
                                                        ).style(f'color: {BRAND["text"]}')
                                                        
                                                        with ui.row().classes('items-center gap-3'):
                                                            ui.label(f"{kr.current:.1f}/{kr.target:.1f}").classes('text-sm font-medium px-2 py-1 rounded').style(
                                                                f'background-color: white; color: {BRAND["text"]}'
                                                            )
                                                            UIComponents.progress_indicator(kr.progress, 'sm', False)
                                                
                                                # Conteúdo expandido do KR
                                                with ui.column().classes('w-full p-5 bg-white gap-5'):
                                                    # Configuração do KR
                                                    with ui.card().classes('w-full p-4 border rounded-lg').style(
                                                        f'border-color: {BRAND["border"]}; background-color: {BRAND["bg_subtle"]}'
                                                    ):
                                                        with ui.row().classes('items-center justify-between mb-3'):
                                                            ui.label('Configuração').classes('text-xs font-semibold uppercase').style(
                                                                f'color: {BRAND["text_light"]}'
                                                            )
                                                            ui.button(icon='delete_outline', on_click=lambda k=kr, o=obj: (
                                                                o.krs.remove(k), 
                                                                state.mark_dirty(), 
                                                                render_management.refresh(),
                                                                ui.notify("Key Result excluído", type="info", position="top")
                                                            )).props('flat dense round').style(f'color: {BRAND["error"]}')
                                                        
                                                        with ui.row().classes('w-full gap-3 items-start'):
                                                            ui.input('Nome', placeholder='Ex: Atingir NPS de 80').bind_value(kr, 'name').on('blur', state.mark_dirty).classes('flex-grow').props('outlined dense bg-white')
                                                            ui.number('Atual', min=0, step=0.1).bind_value(kr, 'current').on('blur', state.mark_dirty).on('change', lambda: render_management.refresh()).classes('w-28').props('outlined dense bg-white')
                                                            ui.number('Meta', min=0, step=0.1).bind_value(kr, 'target').on('blur', state.mark_dirty).on('change', lambda: render_management.refresh()).classes('w-28').props('outlined dense bg-white')
                                                    
                                                    ui.separator()
                                                    
                                                    # Plano de ação - NÍVEL 3
                                                    with ui.row().classes('w-full items-center justify-between mb-3'):
                                                        ui.label('Plano de Ação').classes('text-sm font-semibold').style(f'color: {BRAND["text"]}')
                                                        ui.label(f'{len(kr.tasks)} tarefas').classes('text-xs px-2 py-1 rounded').style(
                                                            f'background-color: {BRAND["bg_subtle"]}; color: {BRAND["text_light"]}'
                                                        )
                                                    
                                                    if not kr.tasks:
                                                        with ui.column().classes('w-full items-center py-8 rounded-lg').style(
                                                            f'background-color: {BRAND["bg_subtle"]}'
                                                        ):
                                                            ui.icon('task_alt', size='md').classes('opacity-20').style(f'color: {BRAND["text_light"]}')
                                                            ui.label('Nenhuma tarefa').classes('text-sm mt-2').style(f'color: {BRAND["text_light"]}')
                                                    else:
                                                        with ui.column().classes('w-full gap-2'):
                                                            for task in kr.tasks:
                                                                status_conf = STATUS_CONFIG.get(task.status, STATUS_CONFIG["Não Iniciado"])
                                                                
                                                                # Card de tarefa - visual leve
                                                                with ui.card().classes('w-full p-4 rounded-lg border').style(
                                                                    f'background-color: {status_conf["bg"]}; border-color: {BRAND["border"]}'
                                                                ):
                                                                    with ui.row().classes('w-full items-center gap-3'):
                                                                        ui.icon(status_conf["icon"], size='sm').style(f'color: {status_conf["color"]}')
                                                                        
                                                                        ui.input(placeholder='Descrever tarefa...').bind_value(task, 'description').on('blur', state.mark_dirty).classes('flex-grow').props('borderless dense').style(
                                                                            f'color: {BRAND["text"]}; font-weight: 500'
                                                                        )
                                                                        
                                                                        s_sel = ui.select(
                                                                            list(STATUS_CONFIG.keys()), 
                                                                            value=task.status,
                                                                            label='Status'
                                                                        ).bind_value(task, 'status').on_value_change(state.mark_dirty)
                                                                        s_sel.classes('w-40').props('outlined dense bg-white')
                                                                        
                                                                        ui.input(placeholder='Responsável', label='Responsável').bind_value(task, 'responsible').on('blur', state.mark_dirty).classes('w-36').props('outlined dense bg-white')

                                                                        with ui.input(placeholder='dd/mm/aaaa', label='Prazo').bind_value(task, 'deadline').on('blur', state.mark_dirty).classes('w-36').props('outlined dense bg-white') as d:
                                                                            with d.add_slot('append'):
                                                                                ui.icon('event', size='sm').on('click', lambda: date_menu.open()).classes('cursor-pointer')
                                                                            with ui.menu() as date_menu:
                                                                                ui.date().bind_value(d).on_value_change(lambda: (date_menu.close(), state.mark_dirty()))
                                                                        
                                                                        ui.button(icon='close', on_click=lambda t=task, k=kr: (
                                                                            k.tasks.remove(t), 
                                                                            state.mark_dirty(), 
                                                                            render_management.refresh()
                                                                        )).props('flat round dense').style(f'color: {BRAND["error"]}')
                                                    
                                                    ui.button('Adicionar tarefa', icon='add_task', on_click=lambda k=kr: (
                                                        k.tasks.append(Task()), 
                                                        render_management.refresh()
                                                    )).props('flat').classes('w-full').style(f'color: {BRAND["primary"]}')

                                        ui.button('Adicionar Key Result', icon='add_circle_outline', on_click=lambda o=obj: (
                                            o.krs.append(KeyResult(name="Novo Key Result")), 
                                            render_management.refresh()
                                        )).props('flat').classes('mt-2').style(f'color: {BRAND["secondary"]}')

@ui.refreshable
def render_dashboard(state: OKRState):
    df = state.to_dataframe()
    if df.empty or (len(df) == 1 and df['kr'].iloc[0] == ""):
        UIComponents.empty_state(
            'insights',
            'Dashboard vazio',
            'Configure objetivos e key results para visualizar análises',
            'Começar',
            lambda: (content.clear(), render_management(state))
        )
        return

    UIComponents.section_title("Visão Geral", "Acompanhe o progresso estratégico", "insights")
    
    df_krs = df[df['kr'] != ''].copy()
    df_krs['pct'] = np.clip(df_krs['avanco'] / df_krs['alvo'].replace(0, 1), 0, 1)
    
    # KPIs simplificados - mais executivos
    with ui.row().classes('w-full gap-4 mb-8'):
        def kpi_card(title, value, subtitle, icon, color):
            with ui.card().classes('flex-1 p-6 rounded-xl border').style(
                f'border-color: {BRAND["border"]}'
            ):
                with ui.row().classes('w-full items-start justify-between mb-3'):
                    ui.icon(icon, size='lg').style(f'color: {color}')
                    ui.label(value).classes('text-4xl font-bold').style(f'color: {color}')
                ui.label(title).classes('text-sm font-semibold').style(f'color: {BRAND["text"]}')
                ui.label(subtitle).classes('text-xs mt-1').style(f'color: {BRAND["text_light"]}')

        avg_progress = df_krs['pct'].mean()
        completed = len(df_krs[df_krs['pct'] >= 1])
        total_krs = len(df_krs)
        in_progress = len(df[df['status'] == 'Em Andamento'])
        
        kpi_card('Progresso Médio', f"{avg_progress*100:.0f}%", 'Todos os Key Results', 'trending_up', BRAND['primary'])
        kpi_card('Taxa de Conclusão', f"{completed}/{total_krs}", f'{(completed/total_krs*100):.0f}% completos', 'check_circle', BRAND['success'])
        kpi_card('Em Execução', str(in_progress), 'Tarefas ativas', 'pending_actions', BRAND['secondary'])

    # Gráficos limpos
    with ui.row().classes('w-full gap-4 mb-6'):
        with UIComponents.card_container(elevated=True).classes('flex-1 h-[380px]'):
            with ui.column().classes('w-full h-full gap-3'):
                ui.label('Status das Ações').classes('text-lg font-bold').style(f'color: {BRAND["text"]}')
                if not df_krs.empty:
                    fig = px.pie(
                        df_krs, 
                        names='status', 
                        color='status', 
                        color_discrete_map={k: v['color'] for k, v in STATUS_CONFIG.items()},
                        hole=0.4
                    )
                    fig.update_traces(textposition='outside', textinfo='percent+label')
                    fig.update_layout(
                        margin=dict(t=10, b=10, l=10, r=10), 
                        showlegend=False,
                        font=dict(size=12)
                    )
                    ui.plotly(fig).classes('w-full h-full')
            
        with UIComponents.card_container(elevated=True).classes('flex-1 h-[380px]'):
            with ui.column().classes('w-full h-full gap-3'):
                ui.label('Progresso por Área').classes('text-lg font-bold').style(f'color: {BRAND["text"]}')
                if not df_krs.empty:
                    df_dept = df_krs.groupby('departamento')['pct'].mean().reset_index()
                    df_dept['pct_label'] = (df_dept['pct'] * 100).round(0).astype(str) + '%'
                    fig2 = px.bar(
                        df_dept, 
                        x='pct', 
                        y='departamento', 
                        orientation='h', 
                        color='pct',
                        color_continuous_scale=[[0, BRAND['error']], [0.5, BRAND['warning']], [1, BRAND['success']]],
                        text='pct_label'
                    )
                    fig2.update_traces(textposition='outside', marker_line_width=0)
                    fig2.update_layout(
                        margin=dict(t=10, b=10, l=10, r=10), 
                        showlegend=False,
                        xaxis_title="",
                        yaxis_title="",
                        coloraxis_showscale=False,
                        font=dict(size=12),
                        xaxis=dict(range=[0, 1.1])
                    )
                    ui.plotly(fig2).classes('w-full h-full')

def export_excel(state: OKRState):
    df = state.to_dataframe()
    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False)
    ui.download(output.getvalue(), f'OKRs_{state.user["cliente"]}.xlsx')
    ui.notify("Relatório exportado", type="positive", color=BRAND['success'], icon="download", position="top")

# --- 6. APP LAYOUT ---

@ui.page('/')
def main_page():
    user_info = app.storage.user.get('user_info')
    if not user_info:
        ui.navigate.to('/login')
        return

    state = OKRState(user_info)

    ui.colors(primary=BRAND['primary'], secondary=BRAND['secondary'], accent=BRAND['accent'], positive=BRAND['success'])

    # Header limpo
    with ui.header().classes('bg-white shadow-sm px-6 py-4').style(f'border-bottom: 1px solid {BRAND["border"]}'):
        with ui.row().classes('w-full max-w-7xl mx-auto items-center justify-between'):
            with ui.row().classes('items-center gap-4'):
                ui.button(icon='menu', on_click=lambda: drawer.toggle()).props('flat round')
                ui.label('OKR Manager').classes('text-xl font-bold').style(f'color: {BRAND["primary"]}')
                ui.separator().props('vertical').classes('h-6')
                ui.badge(user_info['cliente'], color='transparent').classes('text-xs px-3 py-1 rounded-full').style(
                    f'background-color: {BRAND["bg_subtle"]}; color: {BRAND["text"]}'
                )
            
            with ui.row().classes('items-center gap-3'):
                # Indicador de não salvo mais visível
                save_btn = ui.button('Salvar', icon='save', on_click=state.save)
                save_btn.style(
                    f'background-color: {BRAND["success"]}; color: white; font-weight: 600;'
                ).props('no-caps unelevated')
                save_btn.bind_visibility_from(state, 'is_dirty')
                
                with ui.avatar(size='36px').style(
                    f'background-color: {BRAND["primary"]}; color: white; font-weight: 600;'
                ):
                    ui.label(user_info['name'][0].upper())
                
                with ui.button(icon='expand_more', on_click=None).props('flat round'):
                    with ui.menu():
                        with ui.column().classes('p-2 min-w-48'):
                            ui.label(user_info['name']).classes('text-sm font-semibold px-3 py-2').style(f'color: {BRAND["text"]}')
                            ui.label(user_info['username']).classes('text-xs px-3 pb-2').style(f'color: {BRAND["text_light"]}')
                            ui.separator()
                            with ui.menu_item(on_click=lambda: (app.storage.user.clear(), ui.navigate.to('/login'))):
                                with ui.row().classes('items-center gap-2'):
                                    ui.icon('logout', size='sm').style(f'color: {BRAND["error"]}')
                                    ui.label('Sair').style(f'color: {BRAND["error"]}')

    # Drawer limpo
    with ui.left_drawer(value=True).classes('p-0').style(
        f'background-color: white; border-right: 1px solid {BRAND["border"]}; width: 260px;'
    ) as drawer:
        with ui.column().classes('w-full h-full'):
            with ui.column().classes('p-6 border-b').style(f'border-color: {BRAND["border"]}'):
                ui.label('NAVEGAÇÃO').classes('text-xs font-bold mb-3').style(f'color: {BRAND["text_light"]}')
                
                def navigate_to(view_func, label):
                    content.clear()
                    with content: view_func(state)

                with ui.column().classes('w-full gap-1'):
                    ui.button('Gestão de OKRs', icon='flag', on_click=lambda: navigate_to(render_management, 'Gestão')).classes(
                        'w-full justify-start px-4 py-3 rounded-lg'
                    ).props('flat no-caps').style(f'color: {BRAND["text"]}')
                    
                    ui.button('Visão Geral', icon='insights', on_click=lambda: navigate_to(render_dashboard, 'Dashboard')).classes(
                        'w-full justify-start px-4 py-3 rounded-lg'
                    ).props('flat no-caps').style(f'color: {BRAND["text"]}')
            
            ui.space()
            
            with ui.column().classes('p-6 border-t').style(f'border-color: {BRAND["border"]}'):
                ui.label('EXPORTAR').classes('text-xs font-bold mb-3').style(f'color: {BRAND["text_light"]}')
                ui.button('Baixar Excel', icon='download', on_click=lambda: export_excel(state)).classes(
                    'w-full justify-start px-4 py-3 rounded-lg'
                ).props('outline no-caps').style(f'color: {BRAND["success"]}; border-color: {BRAND["success"]}')

    # Conteúdo principal
    content = ui.column().classes('w-full max-w-7xl mx-auto p-8 flex-grow')
    with content:
        render_management(state)

# --- 7. INICIALIZAÇÃO ---
if __name__ in {"__main__", "__mp_main__"}:
    ui.run(
        title="OKR Manager",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8080)),
        storage_secret=os.getenv("STORAGE_SECRET", "super-secret-key-123"),
        language="pt-BR",
        favicon="🎯"
    )
