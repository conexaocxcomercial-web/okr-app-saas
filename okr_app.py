import os
from dataclasses import dataclass, field
from typing import List, Optional
from uuid import uuid4
import pandas as pd
from sqlalchemy import create_engine, text
from nicegui import ui

# --- 1. CONFIGURAÇÃO DE AMBIENTE ---
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///okr_local.db")

if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# --- 2. MODELOS DE DOMÍNIO ---
@dataclass
class Task:
    id: str = field(default_factory=lambda: str(uuid4()))
    description: str = ""
    status: str = "Não Iniciado"
    responsible: str = ""
    deadline: Optional[str] = None 
    _callback: Optional[callable] = None

    def mark_dirty(self):
        if self._callback: self._callback()

@dataclass
class KeyResult:
    name: str
    target: float = 1.0
    current: float = 0.0
    tasks: List[Task] = field(default_factory=list)
    _callback: Optional[callable] = None

    @property
    def progress_pct(self) -> float:
        if self.target == 0: return 1.0 if self.current >= 0 else 0.0
        val = self.current / self.target
        return min(max(val, 0.0), 1.0)

    def add_task(self, task: Task):
        task._callback = self.notify
        self.tasks.append(task)
        self.notify()

    def remove_task(self, task: Task):
        if task in self.tasks:
            self.tasks.remove(task)
            self.notify()

    def notify(self):
        if self._callback: self._callback()

@dataclass
class Objective:
    id: str = field(default_factory=lambda: str(uuid4()))
    department: str = "Geral"
    name: str = ""
    krs: List[KeyResult] = field(default_factory=list)
    _app_callback: Optional[callable] = None

    @property
    def progress_avg(self) -> float:
        if not self.krs: return 0.0
        return sum(kr.progress_pct for kr in self.krs) / len(self.krs)

    def add_kr(self, kr: KeyResult):
        kr._callback = self.notify
        self.krs.append(kr)
        self.notify()

    def notify(self):
        if self._app_callback: self._app_callback()

# --- 3. PERSISTÊNCIA ---
class Persistence:
    def __init__(self, db_url):
        self.engine = create_engine(db_url)
        self._init_tables()

    def _init_tables(self):
        with self.engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS okrs (
                    departamento TEXT, objetivo TEXT, kr TEXT, 
                    tarefa TEXT, status TEXT, responsavel TEXT, prazo TEXT,
                    avanco REAL, alvo REAL, cliente TEXT
                )
            """))

    def load(self) -> pd.DataFrame:
        try:
            with self.engine.connect() as conn:
                return pd.read_sql("SELECT * FROM okrs", conn)
        except Exception as e:
            print(f"Erro crítico: {e}")
            return pd.DataFrame()

    def save(self, df: pd.DataFrame):
        with self.engine.begin() as conn:
            conn.execute(text("DELETE FROM okrs")) 
            if not df.empty:
                df.to_sql('okrs', conn, if_exists='append', index=False)

# --- 4. GERENCIADOR DE ESTADO ---
class AppState:
    def __init__(self):
        self.db = Persistence(DATABASE_URL)
        self.objectives: List[Objective] = []
        self._dirty = False
        self.btn_save_ref = None

    def load_data(self):
        df = self.db.load()
        self.objectives = self._df_to_domain(df)
        self.dirty = False

    def save_data(self):
        if not self.dirty: return
        ui.notify("Salvando...", type='info')
        try:
            df = self._domain_to_df(self.objectives)
            self.db.save(df)
            self.dirty = False
            ui.notify("Salvo!", type='positive')
        except Exception as e:
            ui.notify(f"Erro: {e}", type='negative')

    @property
    def dirty(self):
        return self._dirty

    @dirty.setter
    def dirty(self, val):
        self._dirty = val
        if self.btn_save_ref:
            self.btn_save_ref.visible = val
            self.btn_save_ref.update()

    def mark_dirty(self):
        if not self.dirty: self.dirty = True

    def _df_to_domain(self, df: pd.DataFrame) -> List[Objective]:
        objs_map = {}
        if df.empty: return []
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

    def _domain_to_df(self, objs: List[Objective]) -> pd.DataFrame:
        data = []
        for o in objs:
            for k in o.krs:
                if not k.tasks:
                    data.append([o.department, o.name, k.name, '', '', '', '', k.current, k.target, 'SaaS'])
                for t in k.tasks:
                    data.append([o.department, o.name, k.name, t.description, t.status, t.responsible, t.deadline, k.current, k.target, 'SaaS'])
            if not o.krs:
                data.append([o.department, o.name, '', '', '', '', '', 0.0, 1.0, 'SaaS'])
        
        cols = ['departamento', 'objetivo', 'kr', 'tarefa', 'status', 'responsavel', 'prazo', 'avanco', 'alvo', 'cliente']
        return pd.DataFrame(data, columns=cols)

state = AppState()
state.load_data()

# --- 5. UI (Interface) ---

def render_task(task: Task, kr: KeyResult, refresh_ui):
    with ui.row().classes('w-full items-center gap-2 p-2 border-b border-gray-100 hover:bg-gray-50'):
        ui.input(value=task.description).bind_value(task, 'description').on('blur', state.mark_dirty).classes('flex-grow').props('dense placeholder="Tarefa"')
        
        def update_color(e):
            state.mark_dirty()
            e.sender.classes(remove='text-red-500 text-green-500 text-yellow-500')
            c = {'Concluído': 'text-green-500', 'Não Iniciado': 'text-red-500'}.get(task.status, 'text-yellow-500')
            e.sender.classes(c)

        opts = ['Não Iniciado', 'Em Andamento', 'Pausado', 'Concluído']
        s = ui.select(opts, value=task.status).bind_value(task, 'status').on_value_change(update_color).classes('w-36 font-bold').props('dense options-dense')
        if task.status == 'Concluído': s.classes('text-green-500')
        elif task.status == 'Não Iniciado': s.classes('text-red-500')
        else: s.classes('text-yellow-500')

        ui.input(value=task.responsible).bind_value(task, 'responsible').on('blur', state.mark_dirty).classes('w-24').props('dense placeholder="Resp."')
        
        with ui.input(value=task.deadline).bind_value(task, 'deadline').on('blur', state.mark_dirty).classes('w-32').props('dense placeholder="Prazo"') as date_in:
            with date_in.add_slot('append'):
                ui.icon('calendar_month').on('click', lambda: date_menu.open()).classes('cursor-pointer text-gray-400')
            with ui.menu() as date_menu:
                ui.date().bind_value(date_in).on_value_change(lambda: (date_menu.close(), state.mark_dirty()))

        ui.button(icon='delete', color='red', on_click=lambda: (kr.remove_task(task), refresh_ui())).props('flat dense round')

# --- AQUI ESTAVA O ERRO: Separamos o Header do Conteúdo ---

@ui.refreshable
def render_content():
    """Esta função só renderiza o conteúdo que muda (abas, cards, listas)"""
    depts = sorted(list(set(o.department for o in state.objectives))) or ["Geral"]
    
    with ui.column().classes('w-full max-w-6xl mx-auto p-4'):
        with ui.tabs().classes('w-full text-blue-600') as tabs:
            for d in depts: ui.tab(d)

        with ui.tab_panels(tabs, value=depts[0]).classes('w-full bg-transparent'):
            for dept in depts:
                with ui.tab_panel(dept):
                    objs = [o for o in state.objectives if o.department == dept]
                    if not objs: ui.label("Vazio por enquanto.").classes('text-gray-400 italic')
                    
                    for obj in objs:
                        with ui.card().classes('w-full mb-4 border-l-4 border-blue-500'):
                            with ui.row().classes('w-full items-center justify-between'):
                                ui.input(value=obj.name).bind_value(obj, 'name').on('blur', state.mark_dirty).classes('text-lg font-bold w-1/2').props('dense')
                                ui.label().bind_text_from(obj, 'progress_avg', lambda x: f"{x*100:.0f}%").classes('text-2xl font-bold text-blue-600')
                            
                            ui.linear_progress(show_value=False).bind_value_from(obj, 'progress_avg').classes('h-2 mb-2')
                            
                            for kr in obj.krs:
                                with ui.expansion(text=kr.name, icon='ads_click').classes('w-full bg-slate-50 mb-2 border rounded').bind_text_from(kr, 'name', lambda x: f"KR: {x} ({kr.progress_pct*100:.0f}%)"):
                                    with ui.column().classes('w-full p-2 bg-white'):
                                        with ui.row().classes('gap-4 mb-2'):
                                            ui.input("KR").bind_value(kr, 'name').on('blur', state.mark_dirty).classes('flex-grow').props('dense')
                                            ui.number("Atual", step=1).bind_value(kr, 'current').on('blur', state.mark_dirty).classes('w-24').props('dense')
                                            ui.number("Meta", step=1).bind_value(kr, 'target').on('blur', state.mark_dirty).classes('w-24').props('dense')
                                        
                                        for t in kr.tasks: render_task(t, kr, render_content.refresh)
                                        ui.button("Nova Tarefa", icon='add', on_click=lambda k=kr: (k.add_task(Task()), render_content.refresh())).props('flat dense size=sm')

                            ui.button("Novo KR", icon='add_circle_outline', on_click=lambda o=obj: (o.add_kr(KeyResult("Novo KR")), render_content.refresh())).props('flat color=blue')

def open_new_obj():
    with ui.dialog() as d, ui.card():
        ui.label('Novo Objetivo')
        dept = ui.input('Departamento', placeholder='Ex: Vendas')
        nome = ui.input('Objetivo', placeholder='Ex: Dobrar Leads')
        def save():
            if dept.value and nome.value:
                state.objectives.append(Objective(department=dept.value, name=nome.value, _app_callback=state.mark_dirty))
                state.mark_dirty()
                render_content.refresh() # Atualiza o conteúdo
                d.close()
        ui.button('Criar', on_click=save)
    d.open()

def setup_ui():
    """Esta função monta o layout fixo (Header) e chama o conteúdo dinâmico"""
    
    # Header Fixo (Fora do Refreshable)
    with ui.header().classes('bg-white text-gray-800 shadow-sm p-4 items-center justify-between'):
        ui.label('OKR SaaS').classes('text-xl font-bold')
        with ui.row():
            state.btn_save_ref = ui.button('Salvar', on_click=state.save_data, icon='cloud_upload').props('color=green')
            state.btn_save_ref.visible = state.dirty
            
            # Note que agora chamamos render_content.refresh()
            ui.button(icon='refresh', on_click=lambda: (state.load_data(), render_content.refresh())).props('flat round')
            ui.button(icon='add', on_click=open_new_obj).props('outline round color=blue')

    # Chama o conteúdo dinâmico
    render_content()

# Inicializa a UI
setup_ui()

# --- 6. STARTUP ---
if __name__ in {"__main__", "__mp_main__"}:
    ui.run(
        title="OKR App",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8080)),
        storage_secret="chave-secreta-do-app",
        language="pt-BR"
    )
