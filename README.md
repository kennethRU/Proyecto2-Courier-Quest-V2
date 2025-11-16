                                                                                Proyecto courier Quest

Este proyecto es un juego desarrollado en Python utilizando la librería Pygame. Incluye módulos para manejar la lógica del juego, inventario, clima, persistencia de datos y una interfaz de usuario.


Características principales:

-Sistema de inventario (inventory.py)
-Módulo de clima dinámico (weather.py)
-Gestión de datos persistentes (persistence.py)
-Utilidades varias (utils.py, sorting.py)
-Interfaz de usuario con Pygame (ui.py)
-Lógica central del juego (game.py, main.py)
-Integración con API externa (api.py)
-Configuración centralizada (config.py)
-Jugador CPU con niveles de dificultad y comportamiento autónomo (easy, medium, hard) (game.py)
-Sistema de stamina mejorado con bloqueo de movimiento y recuperación (game.py)
-Caminos ponderados y recalculo automático de rutas (game.py)
-Reglas de aceptación, entrega y cancelación de pedidos (game.py)


Estructura del Proyecto 

│── api.py              # Manejo de API externas

│── config.py           # Configuración global del proyecto

│── game.py             # Mecánicas principales del juego

│── inventory.py        # Sistema de inventario

│── main.py             # Punto de entrada principal

│── models.py           # Clases y modelos de datos

│── persistence.py      # Guardado y carga de datos

│── sorting.py          # Algoritmos de ordenamiento

│── ui.py               # Interfaz de usuario con Pygame

│── utils.py            # Funciones auxiliares

│── weather.py          # Simulación del clima

│── requirements.txt    # Dependencias del proyecto

│── .venv/              # Entorno virtual de Python


CPU AI — Resumen de Métodos por Dificultad

Nivel Fácil — Heurística aleatoria
-------------------------------
_cpu_easy(dt)
    # Controla el movimiento aleatorio de la CPU, acepta y entrega trabajos si está cerca.
_cpu_try_accept_job_random()
    # Intenta aceptar un trabajo aleatorio disponible adyacente a la CPU.

Nivel Medio — Evaluación simple / estilo greedy
-------------------------------
_cpu_medium(dt)
    # Determina objetivos y mueve la CPU hacia el mejor trabajo evaluado por puntuación simple.
_cpu_try_accept_job_medium_limited()
    # Acepta trabajos limitando la cantidad máxima de trabajos concurrentes de la CPU.
_evaluate_job(job)
    # Evalúa un trabajo con función score = payout - 2*distancia - penalización por clima.
_cpu_set_target(target)
    # Actualiza el objetivo de la CPU y fuerza recálculo de la ruta si cambia.
_cpu_register_move()
    # Registra que la CPU se movió exitosamente (resetea contador de bloqueos).
_cpu_register_no_move()
    # Registra que la CPU no pudo moverse; reinicia la ruta si está atascada.

Nivel Difícil — Optimización basada en grafos
-------------------------------
_cpu_hard(dt)
    # Determina el objetivo y mueve la CPU usando rutas ponderadas (Dijkstra / A*).
_weighted_shortest_path(grid, start, goal)
    # Calcula el camino más corto ponderado entre dos puntos considerando el costo de superficie y clima.
_nearest_accessible(target_rc, grid)
    # Encuentra la celda accesible más cercana a un objetivo bloqueado.

Funciones auxiliares
-------------------------------
_cell_is_blocked_by_type(x, y)
    # Retorna True si la celda (x,y) está bloqueada por un edificio u obstáculo.
_is_blocked(position)
    # Retorna True si la posición está fuera del mapa o bloqueada.


Requisitos 
Python 3.10+
pygame 2.5.2
requests 2.32.3

Instalacion
clonar el repositorio con el commando: git clone
instalar dependencias con el comando: py -m pip install --user -r requirements.txt


Ejecución
Comando por consola: python main.py


Teclas de control del juego
En la primer pantalla del juego(escoger dificultad):
1 : dificultad facil
2 : dificultad media
3 : dificultad dificil 

segunda pantalla (pantalla de juego): 
↑ ↓ ← → : Mover al jugador por la ciudad.
A : Aceptar y recoger el pedido cercano
D : Entregar el pedido actualmente seleccionado en el inventario
c : Cancelar el pedido actualmente seleccionado
[ : Mover el cursor del inventario hacia atras
] : Mover el cursor del inventario hacia adelante
1 : Vista de inventario "natural"
2 : Vista de inventario por "prioridad"
3 : Vista de inventario por "deadline" (hora de entrega)
u : Deshacer acción (hasta 20 pasos)
s : Guardar la partida actual
L : Cargar la partida guardada
R : Reiniciar el juego (solo si el juego terminó)
H : Mostrar historial de partidas (solo si el juego terminó)
ESC : Si estás viendo el historial
 - cerrar historial. Si estás en el juego
 - salir del juego


Desarrollo 
Models → Define las estructuras de datos
Game / UI → Contienen la lógica principal y la interfaz
Persistence → Permite guardar y cargar partidas
Weather → Añade realismo dinámico con clima
Utils / Sorting → Encapsulan funciones auxiliares y algoritmos
Game → Ahora incluye lógica de CPU, stamina dinámica, y rutas ponderadas.

Licencias/Uso
Proyecto de uso academico y libre de modificaciones

Autores/Creditos 
Kenneth Ramirez Ugalde
Fernan Mesen Barboza
