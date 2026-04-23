# 💬 Wapp Sender

Aplicación web moderna para enviar mensajes de difusión personalizados por WhatsApp a través de [WAHA](https://waha.devlike.pro/) (WhatsApp HTTP API).

## ✨ Características

- 🔐 **Autenticación segura** – inicio de sesión con usuario y contraseña
- 👥 **Gestión de listas de contactos** – crea, edita y organiza múltiples listas
- �� **Importación desde Excel** – importa contactos desde archivos `.xlsx`/`.xls` con columnas `name` y `phone`
- ✉️ **Campañas de difusión** – envía mensajes personalizados a una o varias listas a la vez
- 🔤 **Placeholders personalizados** – usa `{{name}}`, `{{phone}}` y cualquier campo extra del Excel en el mensaje
- 📱 **Configuración de WhatsApp** – conecta tu cuenta mostrando el QR directamente en la interfaz
- 📊 **Dashboard** – estadísticas y estado de conexión en tiempo real

## 🚀 Instalación

### Requisitos

- Python 3.10+
- [WAHA](https://waha.devlike.pro/) corriendo en tu servidor (vía Docker)

### Pasos

```bash
# 1. Clona el repositorio
git clone https://github.com/gabrielsaenz20/Wapp-Sender.git
cd Wapp-Sender

# 2. Crea el entorno virtual e instala dependencias
python -m venv .venv
source .venv/bin/activate        # Linux/macOS
# .venv\Scripts\activate         # Windows

pip install -r requirements.txt

# 3. Configura las variables de entorno
cp .env.example .env
# Edita .env y cambia SECRET_KEY, ADMIN_USERNAME y ADMIN_PASSWORD

# 4. Inicia el servidor
uvicorn main:app --host 0.0.0.0 --port 8000
```

Abre `http://localhost:8000` en tu navegador.

### Variables de entorno (`.env`)

| Variable         | Descripción                                      | Por defecto                  |
|------------------|--------------------------------------------------|------------------------------|
| `SECRET_KEY`     | Clave secreta para las sesiones (¡cámbiala!)     | —                            |
| `ADMIN_USERNAME` | Usuario administrador inicial                    | `admin`                      |
| `ADMIN_PASSWORD` | Contraseña administrador inicial                 | `admin123`                   |
| `DATABASE_URL`   | URL de la base de datos SQLite                   | `sqlite:///./wapp_sender.db` |

## 📋 Formato del Excel

El archivo Excel debe tener como mínimo las columnas:

| name        | phone          | ... (extra)     |
|-------------|----------------|-----------------|
| Juan García | 5219981234567  | cualquier campo |
| María López | 5219987654321  | ...             |

Los campos extra se pueden usar como `{{campo}}` en el mensaje.

**Alias aceptados:**
- Para nombre: `name`, `nombre`, `full_name`, `contacto`
- Para teléfono: `phone`, `telefono`, `celular`, `movil`, `tel`, `whatsapp`

## 🐳 WAHA (Docker)

```bash
docker run -it --rm -p 3000:3000 devlikeapro/waha
```

## 🛠️ Tecnologías

- **Backend:** Python + FastAPI + SQLAlchemy + SQLite
- **Frontend:** Bootstrap 5.3 + Bootstrap Icons
- **Excel:** openpyxl
- **Auth:** bcrypt + sesión firmada con itsdangerous
