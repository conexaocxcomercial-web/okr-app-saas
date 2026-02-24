import os
from uuid import uuid4
from typing import List, Optional, Dict
from dataclasses import dataclass, field
import pandas as pd
import numpy as np
from sqlalchemy import create_engine, Column, String, Float, text
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from nicegui import ui, app
import plotly.express as px
from io import BytesIO

# --- 1. CONFIGURAÇÃO ---
DATABASE_URL = os.getenv("DATABASE_URL")

BRAND = {
    "primary": "#4f46e5",
    "secondary": "#8b5cf6",
    "accent": "#10b981",
    "dark": "#0f172a",
    "text": "#1e293b",
    "text_light": "#64748b",
    "bg": "#ffffff",
    "bg_subtle": "#f8fafc",
    "border": "#e2e8f0",
    "success": "#10b981",
    "warning": "#f59e0b",
    "error": "#ef4444"
}

STATUS_CONFIG = {
    "Não Iniciado": {"color": "#ef4444", "icon": "radio_button_unchecked", "bg": "#fef2f2"},
    "Em Andamento": {"color": "#3b82f6", "icon": "pending", "bg": "#eff6ff"},
    "Pausado":       {"color": "#f59e0b", "icon": "pause_circle_outline", "bg": "#fffbeb"},
    "Concluído":     {"color": "#10b981", "icon": "check_circle", "bg": "#f0fdf4"},
}

# --- 2. PERSISTÊNCIA ---
Base = declarative_base()

class UserDB(Base):
    __tablename__ = 'users'
    username = Column(String, primary_key=True)
    password = Column(String)
    name     = Column(String)
    cliente  = Column(String)

class OKRDataDB(Base):
    __tablename__ = 'okr_data'
    id           = Column(String, primary_key=True, default=lambda: str(uuid4()))
    cliente      = Column(String, index=True)
    departamento = Column(String)
    objetivo     = Column(String)
    kr           = Column(String)
    tarefa       = Column(String)
    status       = Column(String)
    responsavel  = Column(String)
    prazo        = Column(String)
    avanco       = Column(Float, default=0.0)
    alvo         = Column(Float, default=1.0)

class DatabaseManager:
    def __init__(self, url):
        self.SessionLocal = None
        self.init_error   = None

        if not url:
            self.init_error = "Variável DATABASE_URL não encontrada."
            print(f"❌ {self.init_error}")
            return

        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)

        try:
            self.engine = create_engine(
                url,
                pool_pre_ping=True,
                pool_recycle=1800,
                connect_args={"connect_timeout": 10, "keepalives": 1},
            )
            Base.metadata.create_all(self.engine)
            self.SessionLocal = sessionmaker(bind=self.engine)
            print("✅ Banco conectado com sucesso!")
        except Exception as e:
            self.init_error = str(e)
            print(f"❌ ERRO CRÍTICO DE CONEXÃO: {e}")

    def get_session(self) -> Session:
        if self.SessionLocal is None:
            raise Exception(f"Banco desconectado: {self.init_error}")
        return self.SessionLocal()

    def login(self, username, password) -> Optional[Dict]:
        try:
            with self.get_session() as s:
                u = s.query(UserDB).filter_by(username=username, password=password).first()
                return {"username": u.username, "name": u.name, "cliente": u.cliente} if u else None
        except:
            return None

    def create_user(self, username, password, name, client) -> tuple[bool, str]:
        try:
            with self.get_session() as s:
                if s.query(UserDB).filter_by(username=username).first():
                    return False, "Usuário já existe"
                s.add(UserDB(username=username, password=password, name=name, cliente=client))
                s.commit()
                return True, "Usuário criado com sucesso"
        except Exception as e:
            return False, str(e)

    def load_client_data(self, client: str) -> pd.DataFrame:
        try:
            if self.SessionLocal is None:
                return pd.DataFrame()
            with self.engine.connect() as conn:
                return pd.read_sql(
                    text("SELECT * FROM okr_data WHERE cliente = :c"),
                    conn, params={'c': client}
                )
        except:
            return pd.DataFrame()

    def sync_data(self, df: pd.DataFrame, client: str) -> bool:
        """
        CORREÇÃO: usa UPSERT em vez de delete+insert total.
        Deleta apenas IDs que sumiram e atualiza/insere os demais.
        """
        try:
            with self.get_session() as s:
                # IDs existentes no banco para esse cliente
                existing_ids = set(
                    row[0] for row in
                    s.execute(text("SELECT id FROM okr_data WHERE cliente = :c"), {"c": client})
                )

                if df.empty:
                    # Remove tudo
                    s.execute(text("DELETE FROM okr_data WHERE cliente = :c"), {"c": client})
                    s.commit()
                    return True

                df = df.copy()
                df['cliente'] = client

                # Garante coluna id
                if 'id' not in df.columns:
                    df['id'] = [str(uuid4()) for _ in range(len(df))]
                else:
                    df['id'] = df['id'].apply(lambda x: x if (isinstance(x, str) and x) else str(uuid4()))

                new_ids = set(df['id'].tolist())

                # Deleta IDs que não existem mais
                to_delete = existing_ids - new_ids
                if to_delete:
                    s.execute(
                        text("DELETE FROM okr_data WHERE id = ANY(:ids)"),
                        {"ids": list(to_delete)}
                    )

                # Upsert em batch (muito mais rápido que row-by-row)
                records = df.to_dict(orient='records')
                s.execute(
                    text("""
                        INSERT INTO okr_data
                            (id, cliente, departamento, objetivo, kr, tarefa, status, responsavel, prazo, avanco, alvo)
                        VALUES
                            (:id, :cliente, :departamento, :objetivo, :kr, :tarefa, :status, :responsavel, :prazo, :avanco, :alvo)
                        ON CONFLICT (id) DO UPDATE SET
                            cliente      = EXCLUDED.cliente,
                            departamento = EXCLUDED.departamento,
                            objetivo     = EXCLUDED.objetivo,
                            kr           = EXCLUDED.kr,
                            tarefa       = EXCLUDED.tarefa,
                            status       = EXCLUDED.status,
                            responsavel  = EXCLUDED.responsavel,
                            prazo        = EXCLUDED.prazo,
                            avanco       = EXCLUDED.avanco,
                            alvo         = EXCLUDED.alvo
                    """),
                    records
                )

                s.commit()
                return True
        except Exception as e:
            print(f"Erro ao salvar: {e}")
            return False

db_manager = DatabaseManager(DATABASE_URL)

# --- 3. DOMÍNIO ---

@dataclass
class Task:
    id:          str           = field(default_factory=lambda: str(uuid4()))
    description: str           = ""
    status:      str           = "Não Iniciado"
    responsible: str           = ""
    deadline:    Optional[str] = None

@dataclass
class KeyResult:
    id:       str        = field(default_factory=lambda: str(uuid4()))
    name:     str        = ""
    target:   float      = 1.0
    current:  float      = 0.0
    tasks:    List[Task] = field(default_factory=list)
    expanded: bool       = False

    @property
    def progress(self) -> float:
        if self.target == 0:
            return 1.0 if self.current >= 0 else 0.0
        return min(max(self.current / self.target, 0.0), 1.0)

@dataclass
class Objective:
    id:         str            = field(default_factory=lambda: str(uuid4()))
    department: str            = "Geral"
    name:       str            = ""
    krs:        List[KeyResult]= field(default_factory=list)
    expanded:   bool           = True

    @property
    def progress(self) -> float:
        if not self.krs:
            return 0.0
        return sum(k.progress for k in self.krs) / len(self.krs)

class OKRState:
    def __init__(self, user_info: Dict):
        self.user               = user_info
        self.objectives: List[Objective] = []
        self.is_dirty:   bool   = False
        self.selected_department: str = "Geral"
        self._df_cache: Optional[pd.DataFrame] = None  # cache do DF com IDs
        self.load()

    def mark_dirty(self):
        self.is_dirty = True

    def load(self):
        df = db_manager.load_client_data(self.user['cliente'])
        self._df_cache = df.copy() if not df.empty else pd.DataFrame()
        self.objectives = self._parse_dataframe(df)
        self.is_dirty   = False
        depts = self.get_departments()
        if self.selected_department not in depts and depts:
            self.selected_department = depts[0]

    def save(self):
        df = self.to_dataframe()
        if db_manager.sync_data(df, self.user['cliente']):
            self._df_cache = df.copy()
            self.is_dirty  = False
            ui.notify("Alterações salvas", type="positive", color=BRAND['success'],
                      icon="check_circle", position="top")
        else:
            err = db_manager.init_error or "Erro de conexão"
            ui.notify(f"Falha ao salvar: {err}", type="negative", position="top")

    def rename_department(self, old_name: str, new_name: str):
        if not new_name:
            return
        for obj in self.objectives:
            if obj.department == old_name:
                obj.department = new_name
        if self.selected_department == old_name:
            self.selected_department = new_name
        self.mark_dirty()

    def delete_department(self, dept_name: str):
        self.objectives = [o for o in self.objectives if o.department != dept_name]
        if self.selected_department == dept_name:
            depts = self.get_departments()
            self.selected_department = depts[0] if depts else "Geral"
        self.mark_dirty()

    def _parse_dataframe(self, df: pd.DataFrame) -> List[Objective]:
        if df.empty:
            return []
        df = df.fillna('')
        objs_dict: Dict = {}

        for _, row in df.iterrows():
            obj_key = (row['departamento'], row['objetivo'])
            if obj_key not in objs_dict:
                objs_dict[obj_key] = Objective(
                    department=row['departamento'],
                    name=row['objetivo']
                )
            obj = objs_dict[obj_key]
            if not row['kr']:
                continue
            kr = next((k for k in obj.krs if k.name == row['kr']), None)
            if not kr:
                kr = KeyResult(
                    name=row['kr'],
                    target=float(row['alvo'] or 1.0),
                    current=float(row['avanco'] or 0.0)
                )
                # Preserve ID from DB if available
                if 'id' in row and row['id']:
                    pass  # ID belongs to row, not KR directly
                obj.krs.append(kr)
            if row['tarefa']:
                task = Task(
                    description=row['tarefa'],
                    status=row['status'],
                    responsible=row['responsavel'],
                    deadline=str(row['prazo'])
                )
                # Preserve task id if we can match
                if 'id' in row and row['id']:
                    task.id = row['id']
                kr.tasks.append(task)

        return list(objs_dict.values())

    def to_dataframe(self) -> pd.DataFrame:
        rows = []
        client = self.user['cliente']
        for obj in self.objectives:
            if not obj.krs:
                rows.append({
                    'id': str(uuid4()), 'departamento': obj.department, 'objetivo': obj.name,
                    'kr': '', 'tarefa': '', 'status': '', 'responsavel': '', 'prazo': '',
                    'avanco': 0.0, 'alvo': 1.0, 'cliente': client
                })
                continue
            for kr in obj.krs:
                if not kr.tasks:
                    rows.append({
                        'id': str(uuid4()), 'departamento': obj.department, 'objetivo': obj.name,
                        'kr': kr.name, 'tarefa': '', 'status': '', 'responsavel': '', 'prazo': '',
                        'avanco': kr.current, 'alvo': kr.target, 'cliente': client
                    })
                    continue
                for task in kr.tasks:
                    rows.append({
                        'id': task.id, 'departamento': obj.department, 'objetivo': obj.name,
                        'kr': kr.name, 'tarefa': task.description, 'status': task.status,
                        'responsavel': task.responsible, 'prazo': task.deadline or '',
                        'avanco': kr.current, 'alvo': kr.target, 'cliente': client
                    })
        return pd.DataFrame(rows) if rows else pd.DataFrame()

    def add_objective(self, department: str, name: str):
        self.objectives.append(Objective(department=department, name=name))
        self.selected_department = department
        self.mark_dirty()

    def remove_objective(self, obj: Objective):
        self.objectives.remove(obj)
        self.mark_dirty()

    def get_departments(self) -> List[str]:
        depts = sorted(set(o.department for o in self.objectives))
        return list(depts) if depts else ["Geral"]

# --- 4. COMPONENTES UI ---

class UIComponents:
    @staticmethod
    def section_title(title: str, subtitle: str = None, icon: str = None):
        with ui.column().classes('gap-2 mb-8'):
            with ui.row().classes('items-center gap-3'):
                if icon:
                    ui.icon(icon, size='md').style(f'color: {BRAND["primary"]}')
                ui.label(title).classes('text-2xl font-bold').style(f'color: {BRAND["text"]}')
            if subtitle:
                ui.label(subtitle).classes('text-sm').style(f'color: {BRAND["text_light"]}')

    @staticmethod
    def empty_state(icon: str, title: str, message: str, action_label=None, action_callback=None):
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
        classes = 'w-full rounded-xl p-6 bg-white'
        classes += ' shadow-sm hover:shadow-md transition-shadow' if elevated else ' border'
        return ui.card().classes(classes).style(f'border-color: {BRAND["border"]}')

    @staticmethod
    def progress_bar_inline(progress: float):
        """Barra de progresso leve — não usa circular (mais leve na renderização)."""
        if progress >= 0.8:
            color = BRAND['success']
        elif progress >= 0.5:
            color = BRAND['warning']
        else:
            color = BRAND['error']
        pct = f"{progress * 100:.0f}%"
        with ui.column().classes('gap-1 items-end'):
            ui.label(pct).classes('text-sm font-bold').style(f'color: {color}')
            with ui.element('div').classes('w-24 h-2 rounded-full').style(f'background: {BRAND["border"]}'):
                ui.element('div').classes('h-2 rounded-full').style(
                    f'width: {pct}; background: {color}; transition: width 0.4s ease;'
                )

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
        success, msg = db_manager.create_user(
            reg_user.value, reg_pass.value, reg_name.value, reg_client.value
        )
        if success:
            ui.notify(msg, type="positive", color=BRAND['success'], position="top")
            tabs.value = 'Login'
        else:
            ui.notify(f"Erro: {msg}", type="negative", position="top")

    def toggle_pw(inp):
        t = inp._props.get('type', 'password')
        inp.props('type=text' if t == 'password' else 'type=password')

    with ui.column().classes('absolute-center w-full max-w-md px-6'):
        with ui.card().classes('w-full shadow-lg rounded-xl overflow-hidden'):
            with ui.column().classes('w-full p-8 items-center justify-center bg-white'):
                ui.label('Gestão de OKR').classes('text-3xl font-black').style(f'color: {BRAND["primary"]}')
                ui.label('Gestão estratégica de objetivos').classes('text-sm mt-1').style(f'color: {BRAND["text_light"]}')

            with ui.column().classes('p-8'):
                with ui.tabs().classes('w-full').props(
                    f'active-color={BRAND["primary"]} indicator-color={BRAND["primary"]}'
                ) as tabs:
                    ui.tab('Login', icon='login')
                    ui.tab('Cadastro', icon='person_add')

                with ui.tab_panels(tabs, value='Login').classes('w-full mt-6'):
                    with ui.tab_panel('Login'):
                        with ui.column().classes('w-full gap-4'):
                            username = ui.input('E-mail', placeholder='seu@email.com').classes('w-full').props('outlined')
                            password = ui.input('Senha', password=True, placeholder='••••••••').classes('w-full').props('outlined type=password')
                            with password.add_slot('append'):
                                ui.icon('visibility').on('click', lambda: toggle_pw(password)).classes('cursor-pointer')
                            ui.button('Entrar', on_click=handle_login, icon='login').classes('w-full mt-2').style(
                                f'background-color: {BRAND["primary"]}; color: white; font-weight: 600;'
                            ).props('no-caps unelevated')

                    with ui.tab_panel('Cadastro'):
                        with ui.column().classes('w-full gap-4'):
                            reg_name   = ui.input('Nome completo', placeholder='João Silva').classes('w-full').props('outlined')
                            reg_client = ui.input('Empresa', placeholder='Nome da sua empresa').classes('w-full').props('outlined')
                            reg_user   = ui.input('E-mail', placeholder='seu@email.com').classes('w-full').props('outlined')
                            reg_pass   = ui.input('Senha', password=True, placeholder='Mínimo 8 caracteres').classes('w-full').props('outlined type=password')
                            with reg_pass.add_slot('append'):
                                ui.icon('visibility').on('click', lambda: toggle_pw(reg_pass)).classes('cursor-pointer')
                            ui.button('Criar conta', on_click=handle_register, icon='person_add').classes('w-full mt-2').style(
                                f'background-color: {BRAND["success"]}; color: white; font-weight: 600;'
                            ).props('no-caps unelevated')


# ─────────────────────────────────────────────
#  COMPONENTES GRANULARES (sem refresh global)
# ─────────────────────────────────────────────

def make_progress_widget(get_progress_fn):
    """
    Cria um widget de progresso isolado por instância.
    Retorna um callable `refresh()` que atualiza APENAS este widget,
    sem afetar nenhum outro na página — elimina o bug de refresh global.
    """
    def _color(p: float) -> str:
        if p >= 0.8: return BRAND['success']
        if p >= 0.5: return BRAND['warning']
        return BRAND['error']

    p0  = get_progress_fn()
    c0  = _color(p0)
    pct0 = f"{p0 * 100:.0f}%"

    with ui.row().classes('items-center gap-2'):
        lbl = ui.label(pct0).classes('text-sm font-bold').style(f'color: {c0}')
        with ui.element('div').classes('w-24 h-2 rounded-full').style(f'background: {BRAND["border"]}'):
            bar = ui.element('div').classes('h-2 rounded-full').style(
                f'width: {pct0}; background: {c0}; transition: width 0.4s ease;'
            )

    def refresh():
        p   = get_progress_fn()
        pct = f"{p * 100:.0f}%"
        c   = _color(p)
        lbl.set_text(pct)
        lbl.style(f'color: {c}')
        bar.style(f'width: {pct}; background: {c}; transition: width 0.4s ease;')

    return refresh



@ui.refreshable
def render_task_list(kr: KeyResult, state: OKRState):
    """Renderiza apenas as tarefas de um KR."""

    def build_task_card(container, task: Task):
        """Constrói um único card de tarefa dentro do container."""
        sc = STATUS_CONFIG.get(task.status, STATUS_CONFIG["Não Iniciado"])
        with container:
            with ui.card().classes('w-full p-4 rounded-lg border task-card').style(
                f'background-color: {sc["bg"]}; border-color: {BRAND["border"]}'
            ) as card:
                with ui.row().classes('w-full items-center gap-3 flex-wrap'):
                    status_icon = ui.icon(sc["icon"], size='sm').style(f'color: {sc["color"]}')

                    ui.input(placeholder='Descrever tarefa...').bind_value(
                        task, 'description'
                    ).on('blur', state.mark_dirty).classes('flex-grow min-w-40').props(
                        'borderless dense'
                    ).style(f'color: {BRAND["text"]}; font-weight: 500')

                    def make_status_handler(t: Task, k: KeyResult, icon_el, card_el):
                        def on_status_change(e):
                            t.status = e.value
                            state.mark_dirty()
                            new_sc = STATUS_CONFIG.get(e.value, STATUS_CONFIG["Não Iniciado"])
                            # Atualiza apenas ícone e cor do card — sem rebuild
                            icon_el.props(f'name={new_sc["icon"]}')
                            icon_el.style(f'color: {new_sc["color"]}')
                            card_el.style(f'background-color: {new_sc["bg"]}; border-color: {BRAND["border"]}')
                            # Progresso não muda com status de tarefa, então não precisa refresh
                        return on_status_change

                    s_sel = ui.select(
                        list(STATUS_CONFIG.keys()),
                        value=task.status,
                        label='Status'
                    ).classes('w-40').props('outlined dense bg-white')
                    s_sel.on_value_change(make_status_handler(task, kr, status_icon, card))

                    ui.input(placeholder='Responsável', label='Responsável').bind_value(
                        task, 'responsible'
                    ).on('blur', state.mark_dirty).classes('w-36').props('outlined dense bg-white')

                    deadline_input = ui.input(
                        placeholder='dd/mm/aaaa', label='Prazo'
                    ).bind_value(task, 'deadline').on('blur', state.mark_dirty).classes('w-36').props(
                        'outlined dense bg-white'
                    )
                    with deadline_input:
                        with ui.menu() as date_menu:
                            ui.date().bind_value(deadline_input).on_value_change(
                                lambda: (date_menu.close(), state.mark_dirty())
                            )
                    deadline_input.on('click', date_menu.open)

                    def make_delete_task(t: Task, k: KeyResult, c):
                        def do_delete():
                            k.tasks.remove(t)
                            state.mark_dirty()
                            c.delete()  # remove só esse card do DOM
                            obj = next((o for o in state.objectives if k in o.krs), None)
                            if obj:
                                render_obj_progress.refresh(obj)
                            render_kr_progress_header.refresh(k)
                        return do_delete

                    ui.button(icon='close', on_click=make_delete_task(task, kr, card)).props(
                        'flat round dense'
                    ).style(f'color: {BRAND["error"]}')

    task_container = ui.column().classes('w-full gap-2')

    if not kr.tasks:
        with task_container:
            with ui.column().classes('w-full items-center py-8 rounded-lg empty-state-tasks').style(
                f'background-color: {BRAND["bg_subtle"]}'
            ):
                ui.icon('task_alt', size='md').classes('opacity-20').style(f'color: {BRAND["text_light"]}')
                ui.label('Nenhuma tarefa').classes('text-sm mt-2').style(f'color: {BRAND["text_light"]}')
    else:
        for task in kr.tasks:
            build_task_card(task_container, task)

    # Botão adicionar: injeta card diretamente no container, sem rebuild
    def add_task():
        # Remove empty state se existir
        for child in list(task_container):
            if hasattr(child, '_classes') and 'empty-state-tasks' in (child._classes or ''):
                child.delete()

        new_task = Task()
        kr.tasks.append(new_task)
        state.mark_dirty()
        build_task_card(task_container, new_task)

    ui.button('Adicionar tarefa', icon='add_task', on_click=add_task).props('flat').classes(
        'w-full mt-2'
    ).style(f'color: {BRAND["primary"]}')


def render_kr_list(obj: Objective, state: OKRState, refresh_obj_progress=None):
    """Renderiza os KRs de um objetivo. Não é @ui.refreshable — usa container direto."""
    if refresh_obj_progress is None:
        refresh_obj_progress = lambda: None

    kr_column = ui.column().classes('w-full mt-5 gap-3')

    def build_and_show_krs():
        kr_column.clear()
        with kr_column:
            if not obj.krs:
                with ui.column().classes('w-full items-center py-10'):
                    ui.icon('analytics', size='lg').classes('opacity-20').style(f'color: {BRAND["text_light"]}')
                    ui.label('Nenhum Key Result').classes('text-sm font-medium mt-3').style(f'color: {BRAND["text"]}')
                    ui.button('Adicionar Key Result', icon='add_circle_outline',
                              on_click=lambda: _add_kr(obj, state, build_and_show_krs, refresh_obj_progress)).props(
                        'flat'
                    ).classes('mt-3').style(f'color: {BRAND["primary"]}')
                return

            for kr in obj.krs:
                def build_kr_block(k: KeyResult, o: Objective):
                    with ui.expansion().classes('w-full rounded-lg overflow-hidden border').style(
                        f'background-color: {BRAND["bg_subtle"]}; border-color: {BRAND["border"]}'
                    ) as exp:
                        exp.bind_value(k, 'expanded')

                        with exp.add_slot('header'):
                            with ui.row().classes('w-full items-center gap-3 px-2'):
                                ui.icon('show_chart', size='sm').style(f'color: {BRAND["secondary"]}')
                                ui.label().bind_text_from(k, 'name', lambda n: n or 'Sem nome').classes(
                                    'font-semibold flex-grow'
                                ).style(f'color: {BRAND["text"]}')
                                with ui.row().classes('items-center gap-3'):
                                    ui.label().bind_text_from(
                                        k, 'current',
                                        lambda c, _k=k: f"{c:.1f}/{_k.target:.1f}"
                                    ).classes('text-sm font-medium px-2 py-1 rounded').style(
                                        f'background-color: white; color: {BRAND["text"]}'
                                    )
                                    # Widget isolado por instância — nunca afeta outros KRs
                                    refresh_kr_progress = make_progress_widget(lambda _k=k: _k.progress)

                        with ui.column().classes('w-full p-5 bg-white gap-5'):
                            with ui.card().classes('w-full p-4 border rounded-lg').style(
                                f'border-color: {BRAND["border"]}; background-color: {BRAND["bg_subtle"]}'
                            ):
                                with ui.row().classes('items-center justify-between mb-3'):
                                    ui.label('Configuração').classes('text-xs font-semibold uppercase').style(
                                        f'color: {BRAND["text_light"]}'
                                    )
                                    def make_delete_kr(k: KeyResult, o: Objective):
                                        def do_delete():
                                            o.krs.remove(k)
                                            state.mark_dirty()
                                            build_and_show_krs()
                                            refresh_obj_progress()
                                            ui.notify("Key Result excluído", type="info", position="top")
                                        return do_delete

                                    ui.button(icon='delete_outline', on_click=make_delete_kr(k, o)).props(
                                        'flat dense round'
                                    ).style(f'color: {BRAND["error"]}')

                                with ui.row().classes('w-full gap-3 items-start'):
                                    ui.input('Nome', placeholder='Ex: Atingir NPS de 80').bind_value(
                                        k, 'name'
                                    ).on('blur', state.mark_dirty).classes('flex-grow').props('outlined dense bg-white')

                                    def make_number_handler(k: KeyResult, rk_fn, ro_fn, attr: str):
                                        def on_blur(e):
                                            try:
                                                val = float(e.sender.value or 0)
                                            except (ValueError, TypeError):
                                                val = 0.0
                                            setattr(k, attr, val)
                                            state.mark_dirty()
                                            rk_fn()  # atualiza APENAS este KR
                                            ro_fn()  # atualiza APENAS este objetivo
                                        return on_blur

                                    ui.number('Atual', min=0, step=0.1).bind_value(
                                        k, 'current'
                                    ).on('blur', make_number_handler(k, refresh_kr_progress, refresh_obj_progress, 'current')).classes(
                                        'w-28'
                                    ).props('outlined dense bg-white')

                                    ui.number('Meta', min=0, step=0.1).bind_value(
                                        k, 'target'
                                    ).on('blur', make_number_handler(k, refresh_kr_progress, refresh_obj_progress, 'target')).classes(
                                        'w-28'
                                    ).props('outlined dense bg-white')

                            ui.separator()

                            with ui.row().classes('w-full items-center justify-between mb-1'):
                                ui.label('Plano de Ação').classes('text-sm font-semibold').style(f'color: {BRAND["text"]}')
                                ui.label().bind_text_from(
                                    k, 'tasks', lambda t: f'{len(t)} tarefas'
                                ).classes('text-xs px-2 py-1 rounded').style(
                                    f'background-color: {BRAND["bg_subtle"]}; color: {BRAND["text_light"]}'
                                )

                            render_task_list(k, state)

                build_kr_block(kr, obj)

            ui.button('Adicionar Key Result', icon='add_circle_outline',
                      on_click=lambda: _add_kr(obj, state, build_and_show_krs, refresh_obj_progress)).props(
                'flat'
            ).classes('mt-2').style(f'color: {BRAND["secondary"]}')

    build_and_show_krs()


def _add_kr(obj: Objective, state: OKRState, rebuild_fn=None, refresh_obj_fn=None):
    obj.krs.append(KeyResult(name="Novo Key Result"))
    state.mark_dirty()
    if rebuild_fn:
        rebuild_fn()
    if refresh_obj_fn:
        refresh_obj_fn()


@ui.refreshable
def render_dept_panel(dept: str, state: OKRState, add_obj_dialog):
    """Renderiza o painel de um único departamento."""
    objs = [o for o in state.objectives if o.department == dept]

    if not objs:
        UIComponents.empty_state(
            'track_changes',
            f'Nenhum objetivo em {dept}',
            'Crie seu primeiro objetivo estratégico',
            'Criar objetivo',
            lambda: add_obj_dialog.open()
        )
        return

    with ui.column().classes('w-full gap-6'):
        def build_obj_block(o: Objective):
            with UIComponents.card_container(elevated=True):
                with ui.row().classes('w-full items-start gap-4 pb-5 border-b').style(
                    f'border-color: {BRAND["border"]}'
                ):
                    with ui.column().classes('flex-grow gap-2'):
                        with ui.row().classes('items-center gap-2 w-full'):
                            ui.icon('flag', size='sm').style(f'color: {BRAND["primary"]}')
                            ui.textarea().bind_value(o, 'name').on(
                                'blur', state.mark_dirty
                            ).classes('text-xl font-bold flex-grow').props(
                                'borderless dense autogrow rows=1'
                            ).style(f'color: {BRAND["text"]}; resize: none;')

                        with ui.row().classes('items-center gap-3 ml-7'):
                            ui.label().bind_text_from(
                                o, 'krs', lambda k: f'{len(k)} KRs'
                            ).classes('text-xs px-2 py-1 rounded').style(
                                f'background-color: {BRAND["bg_subtle"]}; color: {BRAND["text_light"]}'
                            )
                            ui.label().bind_text_from(
                                o, 'krs',
                                lambda k: f'{sum(len(kr.tasks) for kr in k)} tarefas'
                            ).classes('text-xs px-2 py-1 rounded').style(
                                f'background-color: {BRAND["bg_subtle"]}; color: {BRAND["text_light"]}'
                            )

                    with ui.column().classes('items-end gap-1'):
                        # Widget de progresso ISOLADO por instância de objetivo
                        refresh_obj_progress = make_progress_widget(lambda _o=o: _o.progress)

                    with ui.button(icon='more_vert').props('flat round dense'):
                        with ui.menu():
                            def make_delete_obj(o: Objective):
                                def do_delete():
                                    state.remove_objective(o)
                                    render_dept_panel.refresh(dept, state, add_obj_dialog)
                                    ui.notify("Objetivo excluído", type="info", position="top")
                                return do_delete

                            with ui.menu_item(on_click=make_delete_obj(o)):
                                with ui.row().classes('items-center gap-2'):
                                    ui.icon('delete_outline', size='sm').style(f'color: {BRAND["error"]}')
                                    ui.label('Excluir').style(f'color: {BRAND["error"]}')

                render_kr_list(o, state, refresh_obj_progress)

        for obj in objs:
            build_obj_block(obj)


@ui.refreshable
def render_management(state: OKRState):
    depts = state.get_departments()

    # Header
    with ui.row().classes('w-full justify-between items-center mb-8'):
        UIComponents.section_title(
            "Objetivos Estratégicos",
            "Gerencie seus OKRs e acompanhe o progresso",
            "flag"
        )
        with ui.row().classes('gap-2'):
            ui.button('Departamentos', icon='corporate_fare', on_click=lambda: dept_dialog.open()).props(
                'outline'
            ).style(f'color: {BRAND["text_light"]}; border-color: {BRAND["border"]}')
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
                d_sel  = ui.select(
                    depts, label="Departamento",
                    value=state.selected_department if state.selected_department in depts else depts[0]
                ).classes('w-full').props('outlined')
                o_name = ui.input(
                    "Nome do objetivo", placeholder="Ex: Aumentar satisfação dos clientes"
                ).classes('w-full').props('outlined')

                with ui.row().classes('w-full justify-end gap-2 mt-4'):
                    ui.button('Cancelar', on_click=add_obj_dialog.close).props('flat').style(
                        f'color: {BRAND["text_light"]}'
                    )

                    def confirm_add():
                        if o_name.value:
                            state.add_objective(d_sel.value, o_name.value)
                            add_obj_dialog.close()
                            # Refresh apenas no painel do departamento afetado
                            render_dept_panel.refresh(d_sel.value, state, add_obj_dialog)
                            # Atualiza as tabs se for departamento novo
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
                        'corporate_fare', 'Nenhum departamento', 'Departamentos são criados automaticamente'
                    )
                else:
                    with ui.column().classes('w-full gap-2'):
                        for d in depts:
                            with ui.card().classes('w-full p-4 border rounded-lg').style(
                                f'border-color: {BRAND["border"]}'
                            ):
                                with ui.row().classes('w-full items-center gap-3'):
                                    ui.icon('folder', size='sm').style(f'color: {BRAND["primary"]}')
                                    d_input = ui.input(value=d).props('borderless').classes(
                                        'font-medium flex-grow'
                                    ).style(f'color: {BRAND["text"]}')

                                    def handle_rename(new_val, old_val=d):
                                        if new_val and new_val != old_val:
                                            state.rename_department(old_val, new_val)
                                            dept_dialog.close()
                                            render_management.refresh()
                                            ui.notify("Departamento renomeado", type="positive",
                                                      color=BRAND['success'], position="top")

                                    d_input.on('blur', lambda e, i=d_input: handle_rename(i.value))

                                    def make_delete_dept(dept_name: str):
                                        def do_delete():
                                            state.delete_department(dept_name)
                                            dept_dialog.close()
                                            render_management.refresh()
                                            ui.notify("Departamento excluído", type="info", position="top")
                                        return do_delete

                                    ui.button(
                                        icon='delete_outline', on_click=make_delete_dept(d)
                                    ).props('flat dense round').style(f'color: {BRAND["error"]}')

            with ui.row().classes('w-full p-6 border-t gap-2 items-center').style(
                f'border-color: {BRAND["border"]}'
            ):
                new_d_input = ui.input(placeholder='Novo departamento').classes('flex-grow').props('outlined dense')

                def create_dept():
                    if new_d_input.value:
                        state.add_objective(new_d_input.value, "Objetivo Inicial")
                        dept_dialog.close()
                        render_management.refresh()
                        ui.notify("Departamento criado", type="positive", color=BRAND['success'], position="top")

                ui.button('Adicionar', icon='add', on_click=create_dept).style(
                    f'background-color: {BRAND["primary"]}; color: white; font-weight: 600;'
                ).props('no-caps unelevated')

    # ─── TABS com bind estável no estado ───
    # Garante consistência antes de renderizar
    if state.selected_department not in depts and depts:
        state.selected_department = depts[0]

    with ui.tabs().classes('w-full mb-6').props(
        f'active-color={BRAND["primary"]} indicator-color={BRAND["primary"]} dense'
    ).bind_value(state, 'selected_department') as dept_tabs:
        for d in depts:
            ui.tab(d, icon='folder')

    # CORREÇÃO CRÍTICA: cada painel é um componente @ui.refreshable independente.
    # Isso impede que editar um KR mude a aba ativa.
    with ui.tab_panels(dept_tabs).bind_value(state, 'selected_department').classes(
        'w-full bg-transparent'
    ):
        for dept in depts:
            with ui.tab_panel(dept).classes('p-0'):
                render_dept_panel(dept, state, add_obj_dialog)


@ui.refreshable
def render_dashboard(state: OKRState):
    df = state.to_dataframe()
    if df.empty or (len(df) == 1 and df.get('kr', pd.Series([''])).iloc[0] == ""):
        UIComponents.empty_state(
            'insights', 'Dashboard vazio',
            'Configure objetivos e key results para visualizar análises'
        )
        return

    UIComponents.section_title("Visão Geral", "Acompanhe o progresso estratégico", "insights")

    df_krs = df[df['kr'] != ''].copy()
    df_krs['pct'] = np.clip(df_krs['avanco'] / df_krs['alvo'].replace(0, 1), 0, 1)

    with ui.row().classes('w-full gap-4 mb-8'):
        def kpi_card(title, value, subtitle, icon, color):
            with ui.card().classes('flex-1 p-6 rounded-xl border').style(f'border-color: {BRAND["border"]}'):
                with ui.row().classes('w-full items-start justify-between mb-3'):
                    ui.icon(icon, size='lg').style(f'color: {color}')
                    ui.label(value).classes('text-4xl font-bold').style(f'color: {color}')
                ui.label(title).classes('text-sm font-semibold').style(f'color: {BRAND["text"]}')
                ui.label(subtitle).classes('text-xs mt-1').style(f'color: {BRAND["text_light"]}')

        avg_progress = df_krs['pct'].mean() if not df_krs.empty else 0
        completed    = len(df_krs[df_krs['pct'] >= 1]) if not df_krs.empty else 0
        total_krs    = len(df_krs)
        in_progress  = len(df[df['status'] == 'Em Andamento'])

        kpi_card('Progresso Médio', f"{avg_progress*100:.0f}%", 'Todos os Key Results', 'trending_up', BRAND['primary'])
        kpi_card('Taxa de Conclusão', f"{completed}/{total_krs}",
                 f'{(completed/total_krs*100):.0f}% completos' if total_krs else '0% completos',
                 'check_circle', BRAND['success'])
        kpi_card('Em Execução', str(in_progress), 'Tarefas ativas', 'pending_actions', BRAND['secondary'])

    with ui.row().classes('w-full gap-4 mb-6'):
        with UIComponents.card_container(elevated=True).classes('flex-1 h-[380px]'):
            with ui.column().classes('w-full h-full gap-3'):
                ui.label('Status das Ações').classes('text-lg font-bold').style(f'color: {BRAND["text"]}')
                if not df_krs.empty:
                    fig = px.pie(
                        df_krs, names='status', color='status',
                        color_discrete_map={k: v['color'] for k, v in STATUS_CONFIG.items()},
                        hole=0.4
                    )
                    fig.update_traces(textposition='outside', textinfo='percent+label')
                    fig.update_layout(margin=dict(t=10, b=10, l=10, r=10), showlegend=False, font=dict(size=12))
                    ui.plotly(fig).classes('w-full h-full')

        with UIComponents.card_container(elevated=True).classes('flex-1 h-[380px]'):
            with ui.column().classes('w-full h-full gap-3'):
                ui.label('Progresso por Área').classes('text-lg font-bold').style(f'color: {BRAND["text"]}')
                if not df_krs.empty:
                    df_dept = df_krs.groupby('departamento')['pct'].mean().reset_index()
                    df_dept['pct_label'] = (df_dept['pct'] * 100).round(0).astype(str) + '%'
                    fig2 = px.bar(
                        df_dept, x='pct', y='departamento', orientation='h', color='pct',
                        color_continuous_scale=[[0, BRAND['error']], [0.5, BRAND['warning']], [1, BRAND['success']]],
                        text='pct_label'
                    )
                    fig2.update_traces(textposition='outside', marker_line_width=0)
                    fig2.update_layout(
                        margin=dict(t=10, b=10, l=10, r=10), showlegend=False,
                        xaxis_title="", yaxis_title="", coloraxis_showscale=False,
                        font=dict(size=12), xaxis=dict(range=[0, 1.1])
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

    ui.colors(
        primary=BRAND['primary'], secondary=BRAND['secondary'],
        accent=BRAND['accent'], positive=BRAND['success']
    )

    # Header
    with ui.header().classes('bg-white shadow-sm px-6 py-4').style(
        f'border-bottom: 1px solid {BRAND["border"]}'
    ):
        with ui.row().classes('w-full max-w-7xl mx-auto items-center justify-between'):
            with ui.row().classes('items-center gap-4'):
                ui.button(icon='menu', on_click=lambda: drawer.toggle()).props('flat round')
                ui.label('Gestão de OKR').classes('text-xl font-bold').style(f'color: {BRAND["primary"]}')
                ui.separator().props('vertical').classes('h-6')
                ui.badge(user_info['cliente'], color='transparent').classes(
                    'text-xs px-3 py-1 rounded-full'
                ).style(f'background-color: {BRAND["bg_subtle"]}; color: {BRAND["text"]}')

            with ui.row().classes('items-center gap-3'):
                save_btn = ui.button('Salvar', icon='save', on_click=state.save)
                save_btn.style(
                    f'background-color: {BRAND["success"]}; color: white; font-weight: 600;'
                ).props('no-caps unelevated')
                save_btn.bind_visibility_from(state, 'is_dirty')

                with ui.avatar(size='36px').style(
                    f'background-color: {BRAND["primary"]}; color: white; font-weight: 600;'
                ):
                    ui.label(user_info['name'][0].upper())

                with ui.button(icon='expand_more').props('flat round'):
                    with ui.menu():
                        with ui.column().classes('p-2 min-w-48'):
                            ui.label(user_info['name']).classes('text-sm font-semibold px-3 py-2').style(
                                f'color: {BRAND["text"]}'
                            )
                            ui.label(user_info['username']).classes('text-xs px-3 pb-2').style(
                                f'color: {BRAND["text_light"]}'
                            )
                            ui.separator()
                            with ui.menu_item(
                                on_click=lambda: (app.storage.user.clear(), ui.navigate.to('/login'))
                            ):
                                with ui.row().classes('items-center gap-2'):
                                    ui.icon('logout', size='sm').style(f'color: {BRAND["error"]}')
                                    ui.label('Sair').style(f'color: {BRAND["error"]}')

    # Drawer
    with ui.left_drawer(value=True).classes('p-0').style(
        f'background-color: white; border-right: 1px solid {BRAND["border"]}; width: 260px;'
    ) as drawer:
        with ui.column().classes('w-full h-full'):
            with ui.column().classes('p-6 border-b').style(f'border-color: {BRAND["border"]}'):
                ui.label('NAVEGAÇÃO').classes('text-xs font-bold mb-3').style(f'color: {BRAND["text_light"]}')

                def navigate_to(view_func):
                    content.clear()
                    with content:
                        view_func(state)

                with ui.column().classes('w-full gap-1'):
                    ui.button('Gestão de OKRs', icon='flag', on_click=lambda: navigate_to(render_management)).classes(
                        'w-full justify-start px-4 py-3 rounded-lg'
                    ).props('flat no-caps').style(f'color: {BRAND["text"]}')

                    ui.button('Visão Geral', icon='insights', on_click=lambda: navigate_to(render_dashboard)).classes(
                        'w-full justify-start px-4 py-3 rounded-lg'
                    ).props('flat no-caps').style(f'color: {BRAND["text"]}')

            ui.space()

            with ui.column().classes('p-6 border-t').style(f'border-color: {BRAND["border"]}'):
                ui.label('EXPORTAR').classes('text-xs font-bold mb-3').style(f'color: {BRAND["text_light"]}')
                ui.button('Baixar Excel', icon='download', on_click=lambda: export_excel(state)).classes(
                    'w-full justify-start px-4 py-3 rounded-lg'
                ).props('outline no-caps').style(
                    f'color: {BRAND["success"]}; border-color: {BRAND["success"]}'
                )

    # Conteúdo principal
    content = ui.column().classes('w-full max-w-7xl mx-auto p-8 flex-grow')
    with content:
        render_management(state)


# --- 7. INICIALIZAÇÃO ---
if __name__ in {"__main__", "__mp_main__"}:
    ui.run(
        title="Gestão de OKR",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8080)),
        storage_secret=os.getenv("STORAGE_SECRET", "super-secret-key-123"),
        language="pt-BR",
        favicon="🎯",
        reconnect_timeout=30,   # aguarda reconexão WebSocket por 30s (evita "conexão perdida")
    )
