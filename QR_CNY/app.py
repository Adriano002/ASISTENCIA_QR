import streamlit as st
import sqlite3
import pandas as pd
from datetime import datetime
import hashlib
import qrcode
from io import BytesIO
import os
import shutil
from PIL import Image
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm
import tempfile
import zipfile

from streamlit_webrtc import webrtc_streamer, VideoTransformerBase
import cv2
import numpy as np
import av

st.set_page_config(page_title="Asistencia Escolar", page_icon="🏫", layout="wide")
DB_PATH = "asistencia_enterprise.db"
BACKUP_DIR = "backups"

# 1. MIGRACIÓN AUTOMÁTICA


def migrar_base_datos():
    """Verifica y agrega todas las columnas faltantes automáticamente"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # --- TABLA: usuarios ---
        cursor.execute("PRAGMA table_info(usuarios)")
        columnas = [col[1] for col in cursor.fetchall()]
        if 'turno_asignado' not in columnas:
            try: cursor.execute("ALTER TABLE usuarios ADD COLUMN turno_asignado TEXT DEFAULT 'Mañana'")
            except: pass
        if 'permisos' not in columnas:
            try: cursor.execute("ALTER TABLE usuarios ADD COLUMN permisos TEXT DEFAULT ''")
            except: pass
        
        # --- TABLA: alumnos ---
        cursor.execute("PRAGMA table_info(alumnos)")
        columnas = [col[1] for col in cursor.fetchall()]
        if 'turno' not in columnas:
            try: cursor.execute("ALTER TABLE alumnos ADD COLUMN turno TEXT DEFAULT 'Mañana'")
            except: pass
        if 'fecha_registro' not in columnas:
            try: cursor.execute("ALTER TABLE alumnos ADD COLUMN fecha_registro TEXT DEFAULT CURRENT_DATE")
            except: pass
        
        # --- TABLA: asistencias ---
        cursor.execute("PRAGMA table_info(asistencias)")
        columnas = [col[1] for col in cursor.fetchall()]
        if 'turno' not in columnas:
            try: cursor.execute("ALTER TABLE asistencias ADD COLUMN turno TEXT DEFAULT 'Mañana'")
            except: pass
        if 'metodo' not in columnas:
            try: cursor.execute("ALTER TABLE asistencias ADD COLUMN metodo TEXT DEFAULT 'manual'")
            except: pass
        if 'registrado_por' not in columnas:
            try: cursor.execute("ALTER TABLE asistencias ADD COLUMN registrado_por TEXT DEFAULT ''")
            except: pass
        
        # --- TABLA: justificaciones ---
        cursor.execute("PRAGMA table_info(justificaciones)")
        columnas = [col[1] for col in cursor.fetchall()]
        if 'fecha_registro' not in columnas:
            try: cursor.execute("ALTER TABLE justificaciones ADD COLUMN fecha_registro TEXT DEFAULT CURRENT_DATE")
            except: pass
        
        conn.commit()
        conn.close()
    except:
        pass

# ============================================================
# 2. INICIALIZACIÓN DE BASE DE DATOS

def get_conn():
    return sqlite3.connect(DB_PATH)

def hash_pass(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def init_db():
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS usuarios (
            username TEXT PRIMARY KEY, password TEXT, rol TEXT, 
            seccion_asignada TEXT, turno_asignado TEXT DEFAULT 'Mañana', 
            permisos TEXT DEFAULT '')""")
        c.execute("""CREATE TABLE IF NOT EXISTS alumnos (
            dni TEXT PRIMARY KEY, nombres TEXT, apellidos TEXT, 
            grado_seccion TEXT, turno TEXT DEFAULT 'Mañana', 
            fecha_registro TEXT DEFAULT CURRENT_DATE)""")
        c.execute("""CREATE TABLE IF NOT EXISTS asistencias (
            id INTEGER PRIMARY KEY AUTOINCREMENT, dni TEXT, fecha TEXT, 
            hora TEXT, estado TEXT, metodo TEXT DEFAULT 'manual', 
            registrado_por TEXT DEFAULT '', turno TEXT DEFAULT 'Mañana')""")
        c.execute("""CREATE TABLE IF NOT EXISTS justificaciones (
            id INTEGER PRIMARY KEY AUTOINCREMENT, dni TEXT, fecha TEXT, 
            motivo TEXT, registrado_por TEXT, 
            fecha_registro TEXT DEFAULT CURRENT_DATE)""")
        c.execute("""CREATE TABLE IF NOT EXISTS auditoria (
            id INTEGER PRIMARY KEY AUTOINCREMENT, usuario TEXT, 
            accion TEXT, timestamp TEXT)""")
        
        if c.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0] == 0:
            c.executemany("INSERT INTO usuarios VALUES (?,?,?,?,?,?)", [
                ("directivo1", hash_pass("dir123"), "Directivo", "TODAS", 
                 "Mañana", "puerta,ver_todo,justificaciones,admin,alumnos"),
                ("auxiliar1", hash_pass("aux123"), "Auxiliar de Puerta", 
                 "TODAS", "Mañana", "puerta"),
                ("docente_1a", hash_pass("doc123"), "Docente", "1°A", 
                 "Mañana", "justificaciones,ver_mi_aula")
            ])
        conn.commit()

init_db()
migrar_base_datos()

# ============================================================
# 3. FUNCIONES
# ============================================================

def audit(usuario, accion):
    with get_conn() as conn:
        conn.execute("INSERT INTO auditoria VALUES (NULL,?,?,?)", 
                    (usuario, accion, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()

def tiene_permiso(p):
    return p in st.session_state.get('permisos', [])

def listar_alumnos(seccion=None):
    with get_conn() as conn:
        q = "SELECT * FROM alumnos"
        if seccion and seccion != "TODAS":
            q += f" WHERE grado_seccion = '{seccion}'"
        return pd.read_sql(q + " ORDER BY apellidos", conn)

def get_alumno(dni):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM alumnos WHERE dni=?", (dni,)).fetchone()

def add_alumno(dni, n, a, g, t="Mañana"):
    try:
        with get_conn() as conn:
            conn.execute("INSERT OR IGNORE INTO alumnos VALUES (?,?,?,?,?,?)", 
                        (dni,n,a,g,t,datetime.now().strftime("%Y-%m-%d")))
            conn.commit()
            return conn.total_changes > 0
    except:
        return False

def update_alumno(dni, n, a, g, t=None):
    with get_conn() as conn:
        if t:
            conn.execute("UPDATE alumnos SET nombres=?, apellidos=?, grado_seccion=?, turno=? WHERE dni=?", 
                        (n,a,g,t,dni))
        else:
            conn.execute("UPDATE alumnos SET nombres=?, apellidos=?, grado_seccion=? WHERE dni=?", 
                        (n,a,g,dni))
        conn.commit()
        return conn.total_changes > 0

def delete_alumno(dni):
    with get_conn() as conn:
        conn.execute("DELETE FROM asistencias WHERE dni=?", (dni,))
        conn.execute("DELETE FROM justificaciones WHERE dni=?", (dni,))
        conn.execute("DELETE FROM alumnos WHERE dni=?", (dni,))
        conn.commit()
        return True

def get_secciones():
    with get_conn() as conn:
        df = pd.read_sql("SELECT DISTINCT grado_seccion FROM alumnos ORDER BY grado_seccion", conn)
        return df['grado_seccion'].tolist() if not df.empty else []

def get_secciones_turno(turno):
    with get_conn() as conn:
        df = pd.read_sql("SELECT DISTINCT grado_seccion FROM alumnos WHERE turno=? ORDER BY grado_seccion", 
                        conn, params=(turno,))
        return df['grado_seccion'].tolist() if not df.empty else []

def load_excel(df):
    if df.empty:
        return 0, "Archivo vacío"
    df.columns = [c.lower().strip() for c in df.columns]
    req = ['dni','nombres','apellidos','grado_seccion']
    if not all(c in df.columns for c in req):
        return 0, "Faltan columnas: dni, nombres, apellidos, grado_seccion"
    
    tiene_turno = 'turno' in df.columns
    ok = 0
    with get_conn() as conn:
        for _, r in df.iterrows():
            try:
                dni = str(r['dni']).strip()
                n = str(r['nombres']).strip()
                a = str(r['apellidos']).strip()
                g = str(r['grado_seccion']).strip()
                if not all([dni,n,a,g]): continue
                t = 'Mañana'
                if tiene_turno and pd.notna(r['turno']):
                    t = 'Tarde' if str(r['turno']).strip().lower() in ['tarde','pm'] else 'Mañana'
                conn.execute("INSERT OR REPLACE INTO alumnos VALUES (?,?,?,?,?,?)", 
                            (dni,n,a,g,t,datetime.now().strftime("%Y-%m-%d")))
                if conn.total_changes > 0: ok += 1
            except: pass
        conn.commit()
    return ok, f"{ok} alumnos cargados"

def registrar_asistencia(dni, estado=None, metodo='manual', turno=None):
    hoy = datetime.now().strftime("%Y-%m-%d")
    hora = datetime.now().strftime("%H:%M:%S")
    if turno is None:
        with get_conn() as conn:
            r = conn.execute("SELECT turno FROM alumnos WHERE dni=?", (dni,)).fetchone()
            turno = r[0] if r else 'Mañana'
    if estado is None:
        estado = "Puntual" if hora <= "08:15:00" else "Tardanza"
    with get_conn() as conn:
        if conn.execute("SELECT id FROM asistencias WHERE dni=? AND fecha=? AND turno=?", 
                       (dni,hoy,turno)).fetchone():
            return False, f"Ya tiene asistencia en turno {turno}"
        conn.execute("""INSERT INTO asistencias 
            (dni,fecha,hora,estado,metodo,registrado_por,turno) 
            VALUES (?,?,?,?,?,?,?)""",
            (dni,hoy,hora,estado,metodo,st.session_state.user,turno))
        conn.commit()
        return True, f"Registrado como {estado}"

def get_asistencias(fecha, seccion=None):
    q = """SELECT a.*, al.nombres, al.apellidos, al.grado_seccion 
           FROM asistencias a JOIN alumnos al ON a.dni=al.dni WHERE a.fecha=?"""
    params = [fecha]
    if seccion and seccion != "TODAS LAS SECCIONES":
        q += " AND al.grado_seccion=?"
        params.append(seccion)
    with get_conn() as conn:
        return pd.read_sql(q + " ORDER BY a.hora DESC", conn, params=params)

def delete_asistencia(id):
    with get_conn() as conn:
        conn.execute("DELETE FROM asistencias WHERE id=?", (id,))
        conn.commit()
        return conn.total_changes > 0

def add_just(dni, fecha, motivo):
    with get_conn() as conn:
        conn.execute("INSERT INTO justificaciones (dni,fecha,motivo,registrado_por) VALUES (?,?,?,?)",
                    (dni,str(fecha),motivo,st.session_state.user))
        conn.commit()
        return True

def get_just():
    with get_conn() as conn:
        return pd.read_sql("""SELECT j.*, a.apellidos, a.nombres, a.grado_seccion 
            FROM justificaciones j JOIN alumnos a ON j.dni=a.dni 
            ORDER BY j.id DESC""", conn)

def delete_just(id):
    with get_conn() as conn:
        conn.execute("DELETE FROM justificaciones WHERE id=?", (id,))
        conn.commit()
        return conn.total_changes > 0

def get_users():
    with get_conn() as conn:
        return pd.read_sql("SELECT username,rol,seccion_asignada,turno_asignado,permisos FROM usuarios", conn)

def add_user(username, password, rol, seccion, turno, permisos):
    with get_conn() as conn:
        conn.execute("INSERT OR IGNORE INTO usuarios VALUES (?,?,?,?,?,?)",
                    (username,hash_pass(password),rol,seccion,turno,permisos))
        conn.commit()
        return conn.total_changes > 0

def update_user(username, rol, seccion, turno, permisos):
    with get_conn() as conn:
        conn.execute("UPDATE usuarios SET rol=?, seccion_asignada=?, turno_asignado=?, permisos=? WHERE username=?",
                    (rol,seccion,turno,permisos,username))
        conn.commit()
        return conn.total_changes > 0

def delete_user(username):
    if username in ['directivo1','auxiliar1']:
        return False, "No se puede eliminar"
    with get_conn() as conn:
        conn.execute("DELETE FROM usuarios WHERE username=?", (username,))
        conn.commit()
        return conn.total_changes > 0, "Eliminado"

def change_pass(username, new_pass):
    with get_conn() as conn:
        conn.execute("UPDATE usuarios SET password=? WHERE username=?", (hash_pass(new_pass), username))
        conn.commit()
        return conn.total_changes > 0

def get_turno_user(username):
    with get_conn() as conn:
        r = conn.execute("SELECT turno_asignado FROM usuarios WHERE username=?", (username,)).fetchone()
        return r[0] if r else 'Mañana'

def update_turno_user(username, turno):
    with get_conn() as conn:
        conn.execute("UPDATE usuarios SET turno_asignado=? WHERE username=?", (turno, username))
        conn.commit()
        return conn.total_changes > 0

def get_secciones_user(username):
    with get_conn() as conn:
        r = conn.execute("SELECT seccion_asignada, turno_asignado FROM usuarios WHERE username=?", (username,)).fetchone()
        if not r: return []
        if r[0] == 'TODAS':
            return get_secciones_turno(r[1])
        secs = [s.strip() for s in r[0].split(',') if s.strip()]
        if not secs: return []
        with get_conn() as conn2:
            q = f"SELECT DISTINCT grado_seccion FROM alumnos WHERE grado_seccion IN ({','.join(['?']*len(secs))}) AND turno=?"
            df = pd.read_sql(q, conn2, params=secs+[r[1]])
            return df['grado_seccion'].tolist() if not df.empty else []

def resumen_alumno(dni):
    with get_conn() as conn:
        a = conn.execute("SELECT COUNT(*) FROM asistencias WHERE dni=? AND estado IN ('Puntual','Tardanza')", (dni,)).fetchone()[0]
        t = conn.execute("SELECT COUNT(*) FROM asistencias WHERE dni=? AND estado='Tardanza'", (dni,)).fetchone()[0]
        f = conn.execute("SELECT COUNT(*) FROM asistencias WHERE dni=? AND estado='Falta'", (dni,)).fetchone()[0]
        j = conn.execute("SELECT COUNT(*) FROM justificaciones WHERE dni=?", (dni,)).fetchone()[0]
        return {'asistencias':a, 'tardanzas':t, 'faltas':f, 'justificaciones':j, 'faltas_efectivas':max(0,f-j)}

def backup_db():
    if not os.path.exists(BACKUP_DIR): os.makedirs(BACKUP_DIR)
    archivo = os.path.join(BACKUP_DIR, f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db")
    shutil.copy2(DB_PATH, archivo)
    return archivo

def list_backups():
    if not os.path.exists(BACKUP_DIR): return []
    return sorted([f for f in os.listdir(BACKUP_DIR) if f.endswith('.db')], reverse=True)

def restore_backup(nombre):
    ruta = os.path.join(BACKUP_DIR, nombre)
    if os.path.exists(ruta):
        shutil.copy2(ruta, DB_PATH)
        return True, "Restaurado"
    return False, "No encontrado"

# ============================================================
# 4. FUNCIONES PARA GENERAR PDF Y ZIP DE QR
# ============================================================

def generar_pdf_qr(seccion, df_alumnos):
    """Genera un PDF con todos los QR de una sección"""
    if df_alumnos.empty:
        return None, "No hay alumnos en esta sección"
    
    pdf_path = tempfile.NamedTemporaryFile(delete=False, suffix='.pdf').name
    c = canvas.Canvas(pdf_path, pagesize=landscape(A4))
    width, height = landscape(A4)
    
    cols = 4
    rows = 3
    qr_size = 60 * mm
    spacing_x = (width - (cols * qr_size)) / (cols + 1)
    spacing_y = (height - (rows * qr_size)) / (rows + 1)
    
    x_pos = spacing_x
    y_pos = height - spacing_y - qr_size
    count = 0
    
    for _, row in df_alumnos.iterrows():
        qr = qrcode.QRCode(version=1, box_size=4, border=2)
        qr.add_data(row['dni'])
        qr.make(fit=True)
        img_qr = qr.make_image(fill_color="black", back_color="white")
        
        qr_temp = tempfile.NamedTemporaryFile(delete=False, suffix='.png').name
        img_qr.save(qr_temp, "PNG")
        
        c.drawImage(qr_temp, x_pos, y_pos, width=qr_size, height=qr_size)
        
        c.setFont("Helvetica", 10)
        nombre_completo = f"{row['apellidos']}, {row['nombres']}"
        c.drawCentredString(x_pos + qr_size/2, y_pos - 15, nombre_completo[:30])
        c.setFont("Helvetica", 8)
        c.drawCentredString(x_pos + qr_size/2, y_pos - 28, f"DNI: {row['dni']}")
        c.drawCentredString(x_pos + qr_size/2, y_pos - 40, row['grado_seccion'])
        
        count += 1
        x_pos += qr_size + spacing_x
        
        if count % cols == 0:
            x_pos = spacing_x
            y_pos -= qr_size + spacing_y
            
        if count % (cols * rows) == 0 and count < len(df_alumnos):
            c.showPage()
            x_pos = spacing_x
            y_pos = height - spacing_y - qr_size
    
    c.save()
    return pdf_path, f"PDF generado con {len(df_alumnos)} QR"

def generar_zip_qr(seccion, df_alumnos):
    """Genera un ZIP con todos los QR individuales"""
    if df_alumnos.empty:
        return None, "No hay alumnos en esta sección"
    
    zip_path = tempfile.NamedTemporaryFile(delete=False, suffix='.zip').name
    
    with zipfile.ZipFile(zip_path, 'w') as zipf:
        for _, row in df_alumnos.iterrows():
            qr = qrcode.QRCode(version=1, box_size=6, border=2)
            qr.add_data(row['dni'])
            qr.make(fit=True)
            img_qr = qr.make_image(fill_color="black", back_color="white")
            
            img_buffer = BytesIO()
            img_qr.save(img_buffer, format="PNG")
            img_buffer.seek(0)
            
            nombre_archivo = f"{row['apellidos']}_{row['nombres']}_{row['dni']}.png".replace(" ", "_")
            zipf.writestr(nombre_archivo, img_buffer.getvalue())
    
    return zip_path, f"ZIP generado con {len(df_alumnos)} QR"


# ============================================================
# 5. FUNCIÓN PARA ESCANEAR QR (CON OPENCV - SIN PYZBAR)
# ============================================================

def procesar_qr(imagen_bytes):
    """Procesa una imagen y extrae el código QR usando OpenCV"""
    try:
        # Convertir bytes a imagen numpy
        nparr = np.frombuffer(imagen_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        
        # Detector QR de OpenCV
        detector = cv2.QRCodeDetector()
        data, bbox, _ = detector.detectAndDecode(img)
        
        if data:
            return data.strip(), None
        return None, "No se detectó ningún código QR"
    except Exception as e:
        return None, f"Error al leer: {e}"


    # ============================================================
# 5.1 CLASE PARA CÁMARA EN TIEMPO REAL
# ============================================================


# ============================================================
# CLASE PARA CÁMARA EN VIVO (CORREGIDA - GUARDA EN SESSION_STATE)
# ============================================================

class QRVideoTransformer:
    def __init__(self):
        self.dni_detectado = None
        self.detector = cv2.QRCodeDetector()
        self.ultimo_dni = None
    
    def recv(self, frame):
        """Procesa cada frame de video"""
        img = frame.to_ndarray(format="bgr24")
        
        # Detectar QR
        data, bbox, _ = self.detector.detectAndDecode(img)
        
        if data:
            self.dni_detectado = data.strip()
            # Guardar en session_state para que la app lo vea
            if self.dni_detectado != self.ultimo_dni:
                self.ultimo_dni = self.dni_detectado
                st.session_state.dni_qr_vivo = self.dni_detectado
            
            # Dibujar rectángulo verde
            if bbox is not None:
                bbox = bbox.astype(int)
                for i in range(len(bbox)):
                    cv2.line(img, tuple(bbox[i][0]), tuple(bbox[(i+1) % len(bbox)][0]), (0, 255, 0), 3)
                cv2.putText(img, f"QR: {data}", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
        else:
            self.dni_detectado = None
        
        return av.VideoFrame.from_ndarray(img, format="bgr24")


# ============================================================
# FUNCIÓN PARA MOSTRAR CÁMARA EN VIVO
# ============================================================

# ============================================================
# FUNCIÓN PARA MOSTRAR CÁMARA EN VIVO (CORREGIDA)
# ============================================================

# ============================================================
# FUNCIÓN PARA MOSTRAR CÁMARA EN VIVO (CON CÁMARA TRASERA)
# ============================================================

# ============================================================
# FUNCIÓN PARA MOSTRAR CÁMARA EN VIVO (SOLO TRASERA)
# ============================================================

# ============================================================
# FUNCIÓN PARA MOSTRAR CÁMARA EN VIVO (SIN OverconstrainedError)
# ============================================================

def mostrar_camara_vivo():
    """Muestra la cámara en vivo con detección de QR"""
    
    st.subheader("📷 Cámara en Vivo")
    st.caption("Apunta la cámara al QR. Se detectará automáticamente")
    
    # Estado para el QR detectado
    if 'dni_qr_vivo' not in st.session_state:
        st.session_state.dni_qr_vivo = None
    
    # Opción para elegir cámara
    col1, col2 = st.columns([3, 1])
    with col2:
        if st.button("🔄 Cambiar cámara", use_container_width=True):
            if 'camara_trasera' not in st.session_state:
                st.session_state.camara_trasera = True
            st.session_state.camara_trasera = not st.session_state.camara_trasera
            st.rerun()
    
    # Determinar facingMode (sin exact para evitar error)
    if 'camara_trasera' not in st.session_state:
        st.session_state.camara_trasera = True
    
    if st.session_state.camara_trasera:
        facing_mode = "environment"
        st.info("📷 Usando cámara TRASERA")
    else:
        facing_mode = "user"
        st.info("🤳 Usando cámara FRONTAL")
    
    # Iniciar stream - SIN exact para evitar OverconstrainedError
    webrtc_streamer(
        key="qr-scanner-vivo",
        video_transformer_factory=QRVideoTransformer,
        media_stream_constraints={
            "video": {
                "facingMode": facing_mode,  # ✅ SIN exact
                "width": {"ideal": 640},
                "height": {"ideal": 480},
            },
            "audio": False,
        },
        async_processing=True,
    )
    
    # Mostrar el QR detectado si existe
    if st.session_state.dni_qr_vivo:
        dni = st.session_state.dni_qr_vivo
        
        st.success(f"✅ QR detectado: {dni}")
        
        alumno = get_alumno(dni)
        if alumno:
            st.info(f"👤 {alumno[2]}, {alumno[1]} - {alumno[3]} (Turno: {alumno[4]})")
            
            st.markdown("---")
            st.subheader("📝 Registrar Asistencia")
            
            col1, col2, col3 = st.columns(3)
            with col1:
                if st.button("✅ Puntual", key="vivo_puntual", use_container_width=True):
                    ok, msg = registrar_asistencia(dni, "Puntual", "qr_vivo")
                    if ok:
                        audit(st.session_state.user, f"QR Vivo {dni}")
                        st.success(f"✔ {msg}")
                        st.balloons()
                        st.session_state.dni_qr_vivo = None
                        st.rerun()
                    else:
                        st.warning(f"⚠️ {msg}")
            with col2:
                if st.button("🟡 Tardanza", key="vivo_tardanza", use_container_width=True):
                    ok, msg = registrar_asistencia(dni, "Tardanza", "qr_vivo")
                    if ok:
                        audit(st.session_state.user, f"QR Vivo Tardanza {dni}")
                        st.warning(f"⚠️ {msg}")
                        st.session_state.dni_qr_vivo = None
                        st.rerun()
                    else:
                        st.warning(f"⚠️ {msg}")
            with col3:
                if st.button("🔴 Falta", key="vivo_falta", use_container_width=True):
                    ok, msg = registrar_asistencia(dni, "Falta", "qr_vivo")
                    if ok:
                        audit(st.session_state.user, f"QR Vivo Falta {dni}")
                        st.error(f"❌ {msg}")
                        st.session_state.dni_qr_vivo = None
                        st.rerun()
                    else:
                        st.warning(f"⚠️ {msg}")
        else:
            st.error(f"❌ DNI {dni} no encontrado")
            if st.button("Limpiar", use_container_width=True):
                st.session_state.dni_qr_vivo = None
                st.rerun()
    
    # Botón para reiniciar
    if st.button("🔄 Reiniciar detección", use_container_width=True):
        st.session_state.dni_qr_vivo = None
        st.rerun()

# ============================================================
# 6. SESIÓN
# ============================================================

if 'logged_in' not in st.session_state:
    st.session_state.logged_in = False
    st.session_state.user = None
    st.session_state.rol = None
    st.session_state.permisos = []

# ============================================================
# 7. LOGIN
# ============================================================

if not st.session_state.logged_in:
    st.title("🏫 Control de Asistencia")
    with st.form("login_form"):
        u = st.text_input("Usuario")
        p = st.text_input("Contraseña", type="password")
        if st.form_submit_button("Acceder"):
            with get_conn() as conn:
                r = conn.execute("""SELECT rol, seccion_asignada, turno_asignado, permisos 
                    FROM usuarios WHERE username=? AND password=?""", 
                    (u, hash_pass(p))).fetchone()
            if r:
                st.session_state.logged_in = True
                st.session_state.user = u
                st.session_state.rol = r[0]
                st.session_state.seccion = r[1]
                st.session_state.turno = r[2] if r[2] else "Mañana"
                st.session_state.permisos = r[3].split(',') if r[3] else []
                audit(u, "Login")
                st.rerun()
            else:
                st.error("Credenciales inválidas")
    st.stop()

# ============================================================
# 8. SIDEBAR
# ============================================================

with st.sidebar:
    st.write(f"**👤 {st.session_state.user}**")
    st.write(f"**🎯 {st.session_state.rol}**")
    st.write(f"**🕐 {st.session_state.get('turno','Mañana')}**")
    st.divider()
    if st.button("🚪 Cerrar Sesión"):
        audit(st.session_state.user, "Logout")
        st.session_state.logged_in = False
        st.rerun()

# ============================================================
# 9. CONTENIDO PRINCIPAL
# ============================================================

st.title("📚 Sistema de Asistencia")

# Métricas
with get_conn() as conn:
    total = pd.read_sql("SELECT COUNT(*) as c FROM alumnos", conn)['c'][0]
    hoy = pd.read_sql("SELECT COUNT(*) as c FROM asistencias WHERE fecha=?", 
                     conn, params=(datetime.now().strftime("%Y-%m-%d"),))['c'][0]
    total_asistencias = pd.read_sql("SELECT COUNT(*) as c FROM asistencias", conn)['c'][0]

col1, col2, col3 = st.columns(3)
col1.metric("👥 Alumnos", total)
col2.metric("✅ Asistencias Hoy", hoy)
col3.metric("📊 Total Asistencias", total_asistencias)
st.divider()

# ============================================================
# 10. TABS
# ============================================================

if st.session_state.rol in ["Directivo", "Auxiliar de Puerta"]:
    tabs = st.tabs([
        "🚪 Registrar", "👥 Alumnos", "📋 Semáforo", "📊 Reportes", 
        "📝 Justificaciones", "🖨️ Carnets", "💾 Backup", "⚙️ Admin"
    ])
else:
    tabs = st.tabs(["📋 Mi Aula", "📊 Reportes", "📝 Justificaciones"])

# ============================================================
# 11. TAB: REGISTRAR
# 
# ============================================================

# ============================================================
# 11. TAB: REGISTRAR (CON CÁMARA EN VIVO FUNCIONAL)
# ============================================================

# ============================================================
# 11. TAB: REGISTRAR (SOLO CÁMARA EN VIVO + MANUAL)
# ============================================================

if st.session_state.rol in ["Directivo", "Auxiliar de Puerta"]:
    with tabs[0]:
        if not tiene_permiso("puerta"):
            st.error("Sin permiso")
        else:
            st.subheader("🚪 Registrar Asistencia")
            
            # Opciones de registro (SOLO 2)
            modo = st.radio(
                "Seleccione el método de registro:", 
                ["📷 Cámara en vivo", "📋 Selección Manual"], 
                horizontal=True, 
                key="modo_registro"
            )
            
            # ==========================================
            # OPCIÓN 1: CÁMARA EN VIVO
            # ==========================================
            if modo == "📷 Cámara en vivo":
                mostrar_camara_vivo()
            
            # ==========================================
            # OPCIÓN 2: SELECCIÓN MANUAL
            # ==========================================
            else:
                st.info("📋 Seleccione la sección y el alumno manualmente")
                
                secs = get_secciones_user(st.session_state.user) or get_secciones()
                if not secs:
                    st.warning("Sin secciones asignadas")
                else:
                    sec = st.selectbox("📚 Seleccione la sección:", secs, key="sec_registro_manual")
                    df = listar_alumnos(sec)
                    
                    if df.empty:
                        st.info("No hay alumnos en esta sección")
                    else:
                        df['display'] = df['apellidos'] + ", " + df['nombres'] + " (DNI: " + df['dni'] + ")"
                        alumno_seleccionado = st.selectbox(
                            "👤 Seleccione el alumno:", 
                            df['display'].tolist(), 
                            key="alumno_registro_manual"
                        )
                        
                        if alumno_seleccionado:
                            dni = alumno_seleccionado.split("(DNI: ")[-1].replace(")", "").strip()
                            
                            alumno = get_alumno(dni)
                            if alumno:
                                st.info(f"👤 {alumno[2]}, {alumno[1]} - {alumno[3]} (Turno: {alumno[4]})")
                            
                            st.markdown("---")
                            st.subheader("📝 Registrar Asistencia")
                            
                            col1, col2, col3 = st.columns(3)
                            with col1:
                                if st.button("✅ Puntual", key="manual_puntual", use_container_width=True):
                                    ok, msg = registrar_asistencia(dni, "Puntual", "manual")
                                    if ok:
                                        audit(st.session_state.user, f"Asistencia Manual {dni}")
                                        st.success(f"✔ {msg}")
                                        st.rerun()
                                    else:
                                        st.warning(f"⚠️ {msg}")
                            with col2:
                                if st.button("🟡 Tardanza", key="manual_tardanza", use_container_width=True):
                                    ok, msg = registrar_asistencia(dni, "Tardanza", "manual")
                                    if ok:
                                        audit(st.session_state.user, f"Tardanza Manual {dni}")
                                        st.warning(f"⚠️ {msg}")
                                        st.rerun()
                                    else:
                                        st.warning(f"⚠️ {msg}")
                            with col3:
                                if st.button("🔴 Falta", key="manual_falta", use_container_width=True):
                                    ok, msg = registrar_asistencia(dni, "Falta", "manual")
                                    if ok:
                                        audit(st.session_state.user, f"Falta Manual {dni}")
                                        st.error(f"❌ {msg}")
                                        st.rerun()
                                    else:
                                        st.warning(f"⚠️ {msg}")

# ============================================================
# 12. TAB: ALUMNOS
# ============================================================

if st.session_state.rol in ["Directivo", "Auxiliar de Puerta"]:
    with tabs[1]:
        if not tiene_permiso("alumnos"):
            st.error("Sin permiso")
        else:
            st.subheader("👥 Gestión de Alumnos")
            tabs2 = st.tabs(["📋 Lista", "➕ Agregar", "✏️ Editar", "🗑️ Eliminar", "📤 Carga Masiva", "🔄 Turnos"])
            
            with tabs2[0]:
                sec = st.selectbox("Filtrar:", ["TODAS"] + get_secciones(), key="filtro_lista")
                df = listar_alumnos(sec)
                if df.empty: st.info("Sin alumnos")
                else: st.dataframe(df)
            
            with tabs2[1]:
                with st.form("add_alumno_form"):
                    c1,c2 = st.columns(2)
                    with c1:
                        dni = st.text_input("DNI*")
                        n = st.text_input("Nombres*")
                    with c2:
                        a = st.text_input("Apellidos*")
                        g = st.text_input("Grado*")
                        t = st.selectbox("Turno:", ["Mañana","Tarde"])
                    if st.form_submit_button("Agregar"):
                        if all([dni,n,a,g]):
                            if add_alumno(dni,n,a,g,t):
                                audit(st.session_state.user, f"Agregó {dni}")
                                st.success("✅ Agregado"); st.rerun()
                            else: st.error("DNI ya existe")
                        else: st.error("Todos los campos son obligatorios")
            
            with tabs2[2]:
                sec = st.selectbox("Sección:", get_secciones(), key="edit_sec")
                df = listar_alumnos(sec)
                if not df.empty:
                    df['display'] = df['apellidos'] + ", " + df['nombres']
                    alum = st.selectbox("Alumno:", df['display'].tolist(), key="edit_alum")
                    if alum:
                        row = df[df['display']==alum].iloc[0]
                        with st.form("edit_alumno_form"):
                            c1,c2 = st.columns(2)
                            with c1:
                                n_e = st.text_input("Nombres", value=row['nombres'])
                            with c2:
                                a_e = st.text_input("Apellidos", value=row['apellidos'])
                            g_e = st.text_input("Grado", value=row['grado_seccion'])
                            t_e = st.selectbox("Turno:", ["Mañana","Tarde"], 
                                              index=0 if row.get('turno','Mañana')=='Mañana' else 1)
                            if st.form_submit_button("Actualizar"):
                                if update_alumno(row['dni'], n_e, a_e, g_e, t_e):
                                    audit(st.session_state.user, f"Actualizó {row['dni']}")
                                    st.success("✅ Actualizado"); st.rerun()
                                else: st.error("Error")
            
            with tabs2[3]:
                sec = st.selectbox("Sección:", get_secciones(), key="del_sec")
                df = listar_alumnos(sec)
                if not df.empty:
                    df['display'] = df['apellidos'] + ", " + df['nombres']
                    alum = st.selectbox("Alumno:", df['display'].tolist(), key="del_alum")
                    if alum:
                        dni = df[df['display']==alum]['dni'].iloc[0]
                        st.warning(f"¿Eliminar {alum}?")
                        if st.button("🗑️ Eliminar", key="btn_del_alum"):
                            if delete_alumno(dni):
                                audit(st.session_state.user, f"Eliminó {dni}")
                                st.success("✅ Eliminado"); st.rerun()
            
            with tabs2[4]:
                arch = st.file_uploader("Excel/CSV:", type=['xlsx','csv'], key="upload_excel")
                if arch:
                    try:
                        df = pd.read_csv(arch) if arch.name.endswith('.csv') else pd.read_excel(arch)
                        st.dataframe(df.head(5))
                        if st.button("Cargar", key="btn_cargar_excel"):
                            ok, msg = load_excel(df)
                            st.success(msg)
                            if ok > 0: audit(st.session_state.user, f"Carga masiva: {ok} alumnos"); st.rerun()
                    except Exception as e:
                        st.error(f"Error: {e}")
            
            with tabs2[5]:
                st.subheader("🔄 Cambiar Turno de Sección")
                st.caption("⚠️ IMPORTANTE: Al cambiar el turno de una sección, los usuarios con permisos en esta sección verán automáticamente los cambios")
                
                with get_conn() as conn:
                    df_t = pd.read_sql("SELECT grado_seccion, turno FROM alumnos GROUP BY grado_seccion, turno ORDER BY grado_seccion", conn)
                
                if not df_t.empty:
                    # Mostrar tabla actual
                    st.write("**Secciones y sus turnos actuales:**")
                    st.dataframe(df_t)
                    
                    sec = st.selectbox("Seleccione la sección para cambiar:", df_t['grado_seccion'].unique().tolist(), key="sec_turno")
                    if sec:
                        t_actual = df_t[df_t['grado_seccion']==sec]['turno'].iloc[0]
                        t_nuevo = "Tarde" if t_actual=="Mañana" else "Mañana"
                        
                        st.info(f"Turno actual: **{t_actual}** → Nuevo turno: **{t_nuevo}**")
                        
                        # Mostrar usuarios afectados
                        with get_conn() as conn2:
                            df_usuarios_afectados = pd.read_sql(
                                "SELECT username, rol, turno_asignado FROM usuarios WHERE seccion_asignada LIKE ?", 
                                conn2, params=(f"%{sec}%",)
                            )
                            if not df_usuarios_afectados.empty:
                                st.warning(f"⚠️ Usuarios que verán cambios (tienen permisos en {sec}):")
                                st.dataframe(df_usuarios_afectados)
                        
                        if st.button(f"🔄 Cambiar {sec} a {t_nuevo}", key="btn_cambiar_turno_sec", use_container_width=True):
                            with get_conn() as conn3:
                                conn3.execute("UPDATE alumnos SET turno=? WHERE grado_seccion=?", (t_nuevo, sec))
                                conn3.commit()
                            audit(st.session_state.user, f"Cambió turno de sección {sec} de {t_actual} a {t_nuevo}")
                            st.success(f"✅ Sección {sec} cambiada a turno {t_nuevo}")
                            st.info("💡 Los usuarios con permisos en esta sección verán automáticamente los cambios")
                            st.rerun()
                else:
                    st.info("No hay secciones registradas")

# ============================================================
# 13. TAB: SEMÁFORO
# ============================================================

idx_sema = 2 if st.session_state.rol in ["Directivo", "Auxiliar de Puerta"] else 0
with tabs[idx_sema]:
    st.subheader("📋 Semáforo de Asistencias")
    secs = get_secciones_user(st.session_state.user) or get_secciones()
    if secs:
        sec = st.selectbox("Sección:", secs, key="semaforo_sec")
        df = listar_alumnos(sec)
        if not df.empty:
            data = []
            for _, row in df.iterrows():
                r = resumen_alumno(row['dni'])
                fe = r['faltas_efectivas']
                estado = "🟢 Normal" if fe==0 else "🟡 Alerta" if fe<=2 else "🟠 Riesgo" if fe<=4 else "🔴 Peligro"
                data.append({"Alumno": f"{row['apellidos']}, {row['nombres']}", 
                            "Asistencias": r['asistencias'],
                            "Tardanzas": r['tardanzas'], "Faltas": r['faltas'], 
                            "Justificaciones": r['justificaciones'], 
                            "Faltas Efectivas": fe, "Estado": estado})
            st.dataframe(pd.DataFrame(data))

# ============================================================
# 14. TAB: REPORTES
# ============================================================

idx_rep = 3 if st.session_state.rol in ["Directivo", "Auxiliar de Puerta"] else 1
with tabs[idx_rep]:
    st.subheader("📊 Reporte Diario")
    c1,c2 = st.columns(2)
    with c1:
        fecha = st.date_input("Fecha:", value=datetime.now(), key="fecha_reporte")
    with c2:
        secs = ["TODAS LAS SECCIONES"] + get_secciones()
        sec = st.selectbox("Sección:", secs, key="sec_reporte")
    
    df = get_asistencias(fecha.strftime("%Y-%m-%d"), sec)
    if df.empty:
        st.info("Sin registros")
    else:
        st.write(f"**Total: {len(df)}**")
        c1,c2,c3 = st.columns(3)
        e = df['estado'].value_counts()
        c1.metric("✅ Puntuales", e.get('Puntual',0))
        c2.metric("🟡 Tardanzas", e.get('Tardanza',0))
        c3.metric("🔴 Faltas", e.get('Falta',0))
        st.dataframe(df)
        
        csv = df.to_csv(index=False)
        st.download_button("📥 Descargar CSV", csv, f"reporte_{fecha.strftime('%Y%m%d')}.csv", 
                          "text/csv", key="descargar_reporte")
        
        if tiene_permiso("admin"):
            with st.expander("🗑️ Eliminar Registro"):
                df['display'] = df['apellidos'] + ", " + df['nombres']
                reg = st.selectbox("Seleccionar:", df['display'].tolist(), key="reg_eliminar")
                if reg:
                    id = df[df['display']==reg]['id'].iloc[0]
                    if st.button("Eliminar", key="btn_eliminar_reg"):
                        if delete_asistencia(id):
                            audit(st.session_state.user, f"Eliminó asistencia {id}")
                            st.success("✅ Eliminado"); st.rerun()

# ============================================================
# 15. TAB: JUSTIFICACIONES
# ============================================================

idx_just = 4 if st.session_state.rol in ["Directivo", "Auxiliar de Puerta"] else 2
with tabs[idx_just]:
    st.subheader("📝 Justificaciones")
    
    if tiene_permiso("justificaciones"):
        with st.expander("➕ Nueva", expanded=False):
            with st.form("add_just_form"):
                with get_conn() as conn:
                    df_a = pd.read_sql("SELECT dni, apellidos, nombres, grado_seccion FROM alumnos ORDER BY apellidos", conn)
                if not df_a.empty:
                    df_a['display'] = df_a['apellidos'] + ", " + df_a['nombres'] + f" ({df_a['grado_seccion']})"
                    alum = st.selectbox("Alumno:", df_a['display'].tolist(), key="just_alum")
                    fecha = st.date_input("Fecha:", key="just_fecha")
                    motivo = st.text_area("Motivo:", key="just_motivo")
                    if st.form_submit_button("Registrar"):
                        if motivo:
                            dni = df_a[df_a['display']==alum]['dni'].iloc[0]
                            if add_just(dni, fecha, motivo):
                                audit(st.session_state.user, f"Justificación {dni}")
                                st.success("✅ Registrado"); st.rerun()
                        else: st.error("Motivo obligatorio")
    
    df_j = get_just()
    if df_j.empty:
        st.info("Sin justificaciones")
    else:
        st.dataframe(df_j)
        if tiene_permiso("admin"):
            with st.expander("🗑️ Eliminar"):
                df_j['display'] = df_j['apellidos'] + ", " + df_j['nombres']
                j = st.selectbox("Seleccionar:", df_j['display'].tolist(), key="just_eliminar")
                if j:
                    id = df_j[df_j['display']==j]['id'].iloc[0]
                    if st.button("Eliminar Justificación", key="btn_eliminar_just"):
                        if delete_just(id):
                            audit(st.session_state.user, f"Eliminó justificación {id}")
                            st.success("✅ Eliminado"); st.rerun()

# ============================================================
# 16. TAB: CARNETS (CON DESCARGA MASIVA)
# ============================================================


if st.session_state.rol in ["Directivo", "Auxiliar de Puerta"]:
    with tabs[5]:
        st.subheader("🖨️ Carnets QR")
        st.caption("Genera y descarga los códigos QR de los alumnos por sección")
        
        secs = get_secciones()
        if not secs:
            st.warning("No hay secciones registradas")
        else:
            sec = st.selectbox("Seleccione la sección:", secs, key="carnet_sec")
            df = listar_alumnos(sec)
            
            if df.empty:
                st.info("No hay alumnos en esta sección")
            else:
                st.write(f"**{len(df)} alumnos en {sec}**")
                
                # Botones de descarga masiva
                col1, col2, col3 = st.columns(3)
                
                with col1:
                    if st.button("📄 Descargar PDF con todos los QR", key="btn_pdf_qr", use_container_width=True):
                        with st.spinner("Generando PDF..."):
                            pdf_path, msg = generar_pdf_qr(sec, df)
                            if pdf_path:
                                with open(pdf_path, "rb") as f:
                                    st.download_button(
                                        "📥 Descargar PDF",
                                        f,
                                        f"carnets_{sec}_{datetime.now().strftime('%Y%m%d')}.pdf",
                                        "application/pdf",
                                        key="descargar_pdf"
                                    )
                                st.success(msg)
                            else:
                                st.error(msg)
                
                with col2:
                    if st.button("📦 Descargar ZIP con todos los QR", key="btn_zip_qr", use_container_width=True):
                        with st.spinner("Generando ZIP..."):
                            zip_path, msg = generar_zip_qr(sec, df)
                            if zip_path:
                                with open(zip_path, "rb") as f:
                                    st.download_button(
                                        "📥 Descargar ZIP",
                                        f,
                                        f"carnets_{sec}_{datetime.now().strftime('%Y%m%d')}.zip",
                                        "application/zip",
                                        key="descargar_zip"
                                    )
                                st.success(msg)
                            else:
                                st.error(msg)
                
                with col3:
                    csv = df.to_csv(index=False)
                    st.download_button(
                        "📊 Descargar CSV",
                        csv,
                        f"alumnos_{sec}_{datetime.now().strftime('%Y%m%d')}.csv",
                        "text/csv",
                        key="descargar_csv_carnet"
                    )
                
                st.divider()
                
                # Mostrar los QR individuales en cuadrícula
                st.subheader("Vista previa de carnets")
                st.caption("Cada código QR contiene el DNI del alumno para registro rápido")
                
                cols_qr = 4
                for i in range(0, len(df), cols_qr):
                    cols = st.columns(cols_qr)
                    for j in range(cols_qr):
                        if i + j < len(df):
                            row = df.iloc[i + j]
                            with cols[j]:
                                qr = qrcode.QRCode(version=1, box_size=4, border=2)
                                qr.add_data(row['dni'])
                                qr.make(fit=True)
                                img = qr.make_image(fill_color="black", back_color="white")
                                buff = BytesIO()
                                img.save(buff, format="PNG")
                                
                                st.image(buff.getvalue(), width=100)
                                st.caption(f"{row['apellidos'][:10]}, {row['nombres'][:10]}")
                                st.caption(f"DNI: {row['dni']}")
                
                # --- SOLO AQUÍ APARECE LA DESCARGA INDIVIDUAL ---
                st.divider()
                with st.expander("📥 Descargar QR individual"):
                    if not df.empty:
                        # Crear lista de opciones
                        opciones = []
                        for _, row in df.iterrows():
                            opciones.append({
                                'display': f"{row['apellidos']}, {row['nombres']} (DNI: {row['dni']})",
                                'dni': row['dni']
                            })
                        
                        opciones_display = [o['display'] for o in opciones]
                        alumno_sel = st.selectbox("Seleccione el alumno:", opciones_display, key="qr_individual_carnet")
                        
                        if alumno_sel:
                            # Buscar el DNI
                            dni_seleccionado = None
                            for opt in opciones:
                                if opt['display'] == alumno_sel:
                                    dni_seleccionado = opt['dni']
                                    break
                            
                            if dni_seleccionado:
                                # Buscar el alumno en el DataFrame
                                alumno_data = df[df['dni'] == dni_seleccionado]
                                
                                if not alumno_data.empty:
                                    row = alumno_data.iloc[0]
                                    
                                    # Generar QR
                                    qr = qrcode.QRCode(version=1, box_size=6, border=2)
                                    qr.add_data(row['dni'])
                                    qr.make(fit=True)
                                    img = qr.make_image(fill_color="black", back_color="white")
                                    buff = BytesIO()
                                    img.save(buff, format="PNG")
                                    
                                    col_img, col_btn = st.columns([1, 2])
                                    with col_img:
                                        st.image(buff.getvalue(), width=150)
                                    with col_btn:
                                        st.download_button(
                                            "📥 Descargar QR",
                                            buff.getvalue(),
                                            f"qr_{row['dni']}_{row['apellidos']}.png",
                                            "image/png",
                                            key=f"descargar_qr_{row['dni']}"
                                        )
                                else:
                                    st.error("❌ Alumno no encontrado")
                    else:
                        st.info("No hay alumnos en esta sección")


# ============================================================
# 18. TAB: BACKUP
# ============================================================

if st.session_state.rol in ["Directivo", "Auxiliar de Puerta"]:
    with tabs[7]:
        st.subheader("💾 Backup")
        c1,c2 = st.columns(2)
        with c1:
            if st.button("📤 Crear Backup", key="btn_crear_backup"):
                arch = backup_db()
                st.success(f"✅ {os.path.basename(arch)}")
                with open(arch, "rb") as f:
                    st.download_button("📥 Descargar", f, os.path.basename(arch), 
                                      key="descargar_backup")
        with c2:
            backups = list_backups()
            if backups:
                sel = st.selectbox("Backups:", backups, key="sel_backup")
                if st.button("🔄 Restaurar", key="btn_restaurar_backup"):
                    ok, msg = restore_backup(sel)
                    if ok:
                        st.success(f"✅ {msg}"); st.rerun()
                    else: st.error(msg)
            else:
                st.info("Sin backups")

# ============================================================
# 19. TAB: ADMIN
# ============================================================

if st.session_state.rol in ["Directivo", "Auxiliar de Puerta"]:
    with tabs[7]:
        if not tiene_permiso("admin"):
            st.error("Sin permiso")
        else:
            st.subheader("⚙️ Administración")
            tabs3 = st.tabs(["👤 Usuarios", "🔐 Seguridad", "📋 Auditoría"])
            
            with tabs3[0]:
                st.subheader("Usuarios")
                with st.expander("➕ Nuevo", expanded=False):
                    with st.form("new_user_form"):
                        c1,c2 = st.columns(2)
                        with c1:
                            u = st.text_input("Usuario*")
                            p = st.text_input("Contraseña*", type="password")
                        with c2:
                            r = st.selectbox("Rol", ["Directivo","Auxiliar de Puerta","Docente"])
                            t = st.selectbox("Turno:", ["Mañana","Tarde"])
                            secs = ["TODAS"] + get_secciones()
                            s = st.multiselect("Secciones:", secs, default=["TODAS"])
                        perm = []
                        col1,col2,col3 = st.columns(3)
                        with col1:
                            if st.checkbox("🚪 Puerta"): perm.append("puerta")
                            if st.checkbox("👁️ Ver"): perm.append("ver_todo")
                        with col2:
                            if st.checkbox("📝 Just"): perm.append("justificaciones")
                            if st.checkbox("⚙️ Admin"): perm.append("admin")
                        with col3:
                            if st.checkbox("👥 Alumnos"): perm.append("alumnos")
                        if st.form_submit_button("Crear"):
                            if u and p:
                                sec_str = "TODAS" if "TODAS" in s else ",".join(s)
                                if add_user(u,p,r,sec_str,t,",".join(perm)):
                                    audit(st.session_state.user, f"Creó {u}")
                                    st.success("✅ Creado"); st.rerun()
                                else: st.error("Usuario existe")
                            else: st.error("Campos obligatorios")
                
                df_u = get_users()
                if not df_u.empty:
                    st.dataframe(df_u)
                    
                    st.subheader("🔄 Cambiar Turno de Usuario")
                    usr = st.selectbox("Usuario:", df_u['username'].tolist(), key="usr_turno")
                    if usr:
                        t_actual = get_turno_user(usr)
                        t_nuevo = "Tarde" if t_actual=="Mañana" else "Mañana"
                        st.info(f"Turno actual: **{t_actual}** → Nuevo: **{t_nuevo}**")
                        if st.button(f"Cambiar a {t_nuevo}", key="btn_cambiar_turno_user", use_container_width=True):
                            if update_turno_user(usr, t_nuevo):
                                audit(st.session_state.user, f"Cambió turno de {usr} a {t_nuevo}")
                                st.success(f"✅ Turno de {usr} cambiado a {t_nuevo}")
                                st.rerun()
                            else:
                                st.error("Error al cambiar")
                    
                    st.subheader("🗑️ Eliminar Usuario")
                    elim = [u for u in df_u['username'].tolist() if u not in ['directivo1','auxiliar1']]
                    if elim:
                        usr_e = st.selectbox("Seleccionar:", elim, key="del_user")
                        if st.button("Eliminar Usuario", key="btn_del_user", use_container_width=True):
                            ok, msg = delete_user(usr_e)
                            if ok:
                                audit(st.session_state.user, f"Eliminó {usr_e}")
                                st.success(f"✅ {msg}"); st.rerun()
                            else: st.error(msg)
            
            with tabs3[1]:
                st.subheader("🔐 Cambiar Contraseña")
                with st.form("chg_pass_form"):
                    df_u = get_users()
                    u = st.selectbox("Usuario:", df_u['username'].tolist(), key="usr_pass")
                    n = st.text_input("Nueva*", type="password")
                    c = st.text_input("Confirmar*", type="password")
                    if st.form_submit_button("Cambiar"):
                        if n and len(n)>=4:
                            if n==c:
                                if change_pass(u,n):
                                    audit(st.session_state.user, f"Cambió pass {u}")
                                    st.success("✅ Actualizado")
                                else: st.error("Error")
                            else: st.error("No coinciden")
                        else: st.error("Mínimo 4 caracteres")
            
            with tabs3[2]:
                st.subheader("📋 Auditoría")
                with get_conn() as conn:
                    df_a = pd.read_sql("SELECT * FROM auditoria ORDER BY id DESC LIMIT 100", conn)
                if df_a.empty: st.info("Sin registros")
                else: st.dataframe(df_a)
