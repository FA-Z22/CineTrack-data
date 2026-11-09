import asyncio
import json
import re
import unicodedata

from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

from playwright.async_api import async_playwright


# ============================================================
# CONFIGURACION
# ============================================================

BASE_DIR = Path(__file__).parent

CARTELERA_URL = (
    "https://www.cinemark.com.co/cartelera/neiva"
)

API_BASE = (
    "https://api.cinemark-core.com/"
    "vista/country/co"
)

COMPANY_ID = "5db771be04daec00076df3f5"

CIUDAD = "neiva"
CINE = "Cinemark"
SEDE = "San Pedro Plaza"

ARCHIVO_CACHE = (
    BASE_DIR / "cinemark_cache.json"
)

ARCHIVO_SALIDA = (
    BASE_DIR / "cinemark_neiva_completo.json"
)

# Hoy + 3 días
DIAS_CARTELERA = 4

# No lanzaremos 50 peticiones a la vez.
# Cinco simultáneas es suficiente.
MAX_CONCURRENCIA = 5


# ============================================================
# UTILIDADES
# ============================================================

def ahora_iso():
    return datetime.now().astimezone().isoformat()


def hoy():
    return datetime.now().astimezone().date()


def normalizar(texto):

    texto = texto.lower()

    texto = unicodedata.normalize(
        "NFD",
        texto
    )

    texto = "".join(
        caracter
        for caracter in texto
        if unicodedata.category(caracter) != "Mn"
    )

    texto = re.sub(
        r"[^a-z0-9]+",
        " ",
        texto
    )

    return " ".join(
        texto.split()
    )


def crear_slug(titulo):

    texto = titulo.lower()

    texto = unicodedata.normalize(
        "NFD",
        texto
    )

    texto = "".join(
        caracter
        for caracter in texto
        if unicodedata.category(caracter) != "Mn"
    )

    texto = re.sub(
        r"[^a-z0-9]+",
        "-",
        texto
    )

    return texto.strip("-")


def cargar_json(ruta):

    if not ruta.exists():
        return None

    try:

        return json.loads(
            ruta.read_text(
                encoding="utf-8"
            )
        )

    except Exception:
        return None


def guardar_json(ruta, datos):

    ruta.write_text(
        json.dumps(
            datos,
            ensure_ascii=False,
            indent=4
        ),
        encoding="utf-8"
    )


# ============================================================
# CACHE
# ============================================================

def cargar_cache():

    datos = cargar_json(
        ARCHIVO_CACHE
    )

    if not datos:

        datos = {
            "peliculas": {}
        }

    if "peliculas" not in datos:

        datos["peliculas"] = {}

    # También aprovechamos el JSON completo anterior.
    completo = cargar_json(
        ARCHIVO_SALIDA
    )

    if completo:

        for pelicula in completo.get(
            "peliculas",
            []
        ):

            titulo = pelicula.get(
                "titulo"
            )

            if not titulo:
                continue

            clave = normalizar(
                titulo
            )

            entrada = (
                datos["peliculas"]
                .setdefault(
                    clave,
                    {}
                )
            )

            entrada["titulo"] = titulo

            if pelicula.get("slug"):

                entrada["slug"] = (
                    pelicula["slug"]
                )

            if pelicula.get("movieId"):

                entrada["movieId"] = str(
                    pelicula["movieId"]
                )

            if pelicula.get(
                "diasDisponibles"
            ):

                entrada[
                    "diasDisponibles"
                ] = pelicula[
                    "diasDisponibles"
                ]

    return datos


# ============================================================
# PARSER DE CARTELERA
# ============================================================

def procesar_bloque(lineas):

    estado = "CARTELERA"

    formato = None
    titulo = None
    duracion = None
    clasificacion = None

    textos = []


    for linea in lineas:

        texto = linea.strip()
        mayus = texto.upper()


        if mayus == "PREVENTA":

            estado = "PREVENTA"
            continue


        if mayus == "ESTRENO":

            estado = "ESTRENO"
            continue


        if mayus in [
            "GENERAL",
            "VER HORARIOS"
        ]:

            continue


        if mayus in [
            "2D",
            "3D",
            "XD",
            "IMAX"
        ]:

            formato = mayus
            continue


        resultado = re.match(
            r"^(\d+)\s*min\s*(.*)$",
            texto,
            re.IGNORECASE
        )


        if resultado:

            duracion = int(
                resultado.group(1)
            )

            restante = (
                resultado
                .group(2)
                .strip()
            )

            if restante:

                clasificacion = restante

            continue


        textos.append(
            texto
        )


    if textos:

        titulo = textos[-1]


    if not titulo:

        return None


    return {
        "titulo": titulo,
        "estado": estado,
        "formatoListado": formato,
        "duracionMinutos": duracion,
        "clasificacion": clasificacion
    }


def extraer_peliculas(texto):

    lineas = [
        re.sub(
            r"\s+",
            " ",
            linea.strip()
        )
        for linea in texto.splitlines()
        if linea.strip()
    ]


    inicio = None
    final = None


    for indice, linea in enumerate(
        lineas
    ):

        mayus = linea.upper()


        if (
            "TODAS LAS PELÍCULAS DISPONIBLES"
            in mayus
        ):

            inicio = indice + 1


        if (
            "PRÓXIMOS ESTRENOS"
            in mayus
        ):

            final = indice
            break


    if inicio is None:

        return []


    if final is None:

        final = len(
            lineas
        )


    peliculas = []

    bloque = []


    for linea in lineas[
        inicio:final
    ]:

        if linea.upper() == "CARGANDO":

            if bloque:

                pelicula = (
                    procesar_bloque(
                        bloque
                    )
                )

                if pelicula:

                    peliculas.append(
                        pelicula
                    )

            bloque = []

        else:

            bloque.append(
                linea
            )


    if bloque:

        pelicula = procesar_bloque(
            bloque
        )

        if pelicula:

            peliculas.append(
                pelicula
            )


    return peliculas


# ============================================================
# HEADERS
# ============================================================

def limpiar_headers(headers):

    return {
        clave: valor
        for clave, valor
        in headers.items()

        if not clave.startswith(":")
        and clave.lower() not in [
            "host",
            "content-length",
            "cookie"
        ]
    }


# ============================================================
# DETALLE DE PELICULA
# ============================================================

async def obtener_detalle(
    context,
    slug,
    semaforo
):

    url = (
        "https://www.cinemark.com.co/"
        f"cartelera/neiva/{slug}"
    )


    async with semaforo:

        pagina = (
            await context.new_page()
        )


        try:

            async with pagina.expect_response(
                lambda respuesta:
                    (
                        "api.cinemark-core.com"
                        in respuesta.url
                        and
                        "/dates-session/"
                        in respuesta.url
                    ),
                timeout=30000
            ) as respuesta_info:

                await pagina.goto(
                    url,
                    wait_until=
                        "domcontentloaded",
                    timeout=60000
                )


            respuesta = (
                await respuesta_info.value
            )


            resultado_id = re.search(
                r"/dates-session/(\d+)",
                respuesta.url
            )


            if not resultado_id:

                return None


            movie_id = (
                resultado_id.group(1)
            )


            datos = (
                await respuesta.json()
            )


            dias = [
                dia["day"]

                for dia in datos.get(
                    "days",
                    []
                )

                if dia.get(
                    "isVisible",
                    False
                )
            ]


            headers_originales = (
                await respuesta
                .request
                .all_headers()
            )


            return {
                "movieId":
                    movie_id,

                "diasDisponibles":
                    dias,

                "headers":
                    limpiar_headers(
                        headers_originales
                    )
            }


        except Exception as error:

            print(
                f"Detalle {slug}: {error}"
            )

            return None


        finally:

            await pagina.close()


# ============================================================
# FECHAS
# ============================================================

def fechas_cartelera():

    fecha = hoy()

    return [
        (
            fecha
            +
            timedelta(
                days=indice
            )
        ).isoformat()

        for indice
        in range(
            DIAS_CARTELERA
        )
    ]


def quitar_fechas_pasadas(fechas):

    fecha_hoy = (
        hoy().isoformat()
    )

    return [
        fecha
        for fecha in fechas
        if fecha >= fecha_hoy
    ]


# ============================================================
# CONSULTA API PARA UNA FECHA
# ============================================================

async def consultar_fecha(
    context,
    headers,
    pelicula,
    fecha,
    semaforo
):

    movie_id = pelicula[
        "movieId"
    ]

    slug = pelicula[
        "slug"
    ]


    parametros = {
        "date":
            fecha,

        "companyId":
            COMPANY_ID,

        "midnightSessionStart":
            "23:10",

        "midnightSessionEnd":
            "03:00"
    }


    url = (
        f"{API_BASE}/"
        f"city/{CIUDAD}/"
        f"movie/{movie_id}"
        f"?{urlencode(parametros)}"
    )


    async with semaforo:

        try:

            respuesta = (
                await context.request.get(
                    url,
                    headers=headers,
                    timeout=30000
                )
            )


            if respuesta.status != 200:

                print(
                    pelicula["titulo"],
                    fecha,
                    "->",
                    respuesta.status
                )

                return []


            datos = (
                await respuesta.json()
            )


        except Exception as error:

            print(
                pelicula["titulo"],
                fecha,
                "ERROR:",
                error
            )

            return []


    funciones = []


    for teatro in datos.get(
        "Theater",
        []
    ):

        if teatro.get(
            "Name"
        ) != SEDE:

            continue


        cinema_id = str(
            teatro.get(
                "CinemaId",
                ""
            )
        )


        for formato in teatro.get(
            "Format",
            []
        ):

            idiomas = (
                formato.get(
                    "LangTypes",
                    []
                )
            )

            pantallas = (
                formato.get(
                    "ScreenTypes",
                    []
                )
            )

            tipos_asiento = (
                formato.get(
                    "SeatTypes",
                    []
                )
            )


            for sesion in formato.get(
                "Sessions",
                []
            ):

                if not sesion.get(
                    "IsVisible",
                    True
                ):

                    continue


                hora = sesion.get(
                    "Showtime",
                    ""
                )


                if len(hora) >= 5:

                    hora = hora[:5]


                session_id = str(
                    sesion.get(
                        "SessionId",
                        ""
                    )
                )


                url_compra = (
                    "https://www.cinemark.com.co/"
                    f"compras/{slug}/"
                    f"{cinema_id}/"
                    f"{session_id}"
                )


                funciones.append({
                    "fecha":
                        fecha,

                    "hora":
                        hora,

                    "idioma":
                        idiomas,

                    "formato":
                        pantallas,

                    "tipoAsiento":
                        tipos_asiento,

                    "sala":
                        sesion.get(
                            "ScreenNumber"
                        ),

                    "puestosDisponibles":
                        sesion.get(
                            "SeatsAvailable"
                        ),

                    "sessionId":
                        session_id,

                    "urlCompra":
                        url_compra
                })


    return funciones


# ============================================================
# MAIN
# ============================================================

async def main():

    inicio = datetime.now()

    print()
    print(
        "=========================================="
    )
    print(
        "CINETRACK - CINEMARK NEIVA TURBO"
    )
    print(
        "=========================================="
    )
    print()


    cache = cargar_cache()

    semaforo_paginas = (
        asyncio.Semaphore(3)
    )

    semaforo_api = (
        asyncio.Semaphore(
            MAX_CONCURRENCIA
        )
    )


    async with async_playwright() as p:

        browser = (
            await p.chromium.launch(
                headless=True
            )
        )


        context = (
            await browser.new_context(
                service_workers="block"
            )
        )


        page = (
            await context.new_page()
        )


        # ===================================================
        # 1. CARTELERA UNA SOLA VEZ
        # ===================================================

        print(
            "Cargando cartelera..."
        )


        await page.goto(
            CARTELERA_URL,
            wait_until=
                "domcontentloaded",
            timeout=60000
        )


        # Ya no esperamos 5, 10 o 15 segundos fijos.
        # Continuamos apenas aparezcan las películas.
        try:

            await page.wait_for_function(
                """
                () => {
                    const texto =
                        document.body.innerText;

                    return (
                        texto.includes(
                            "TODAS LAS PELÍCULAS DISPONIBLES"
                        )
                        &&
                        texto.includes(
                            "VER HORARIOS"
                        )
                    );
                }
                """,
                timeout=20000
            )

        except Exception:

            print(
                "La cartelera tardó más de lo esperado."
            )


        texto = (
            await page
            .locator("body")
            .inner_text()
        )


        peliculas_web = (
            extraer_peliculas(
                texto
            )
        )


        print(
            "Películas:",
            len(peliculas_web)
        )


        if not peliculas_web:

            await browser.close()
            return


        # ===================================================
        # 2. PREPARAR PELICULAS
        # ===================================================

        peliculas = []


        for pelicula in peliculas_web:

            titulo = pelicula[
                "titulo"
            ]

            clave = normalizar(
                titulo
            )

            cache_pelicula = (
                cache["peliculas"]
                .get(
                    clave,
                    {}
                )
            )


            slug = (
                cache_pelicula.get(
                    "slug"
                )
                or
                crear_slug(
                    titulo
                )
            )


            peliculas.append({
                **pelicula,

                "clave":
                    clave,

                "slug":
                    slug,

                "movieId":
                    cache_pelicula.get(
                        "movieId"
                    ),

                "diasDisponibles":
                    cache_pelicula.get(
                        "diasDisponibles",
                        []
                    )
            })


        # ===================================================
        # 3. ABRIR PRIMERA PELICULA
        #
        # Solo una página para conseguir
        # headers/sesión válidos.
        # ===================================================

        print(
            "Inicializando sesión Cinemark..."
        )


        primera = peliculas[0]


        detalle_primera = (
            await obtener_detalle(
                context,
                primera["slug"],
                semaforo_paginas
            )
        )


        if not detalle_primera:

            print(
                "No se pudo iniciar sesión."
            )

            await browser.close()
            return


        headers = (
            detalle_primera[
                "headers"
            ]
        )


        primera["movieId"] = (
            detalle_primera[
                "movieId"
            ]
        )


        primera[
            "diasDisponibles"
        ] = detalle_primera[
            "diasDisponibles"
        ]


        # ===================================================
        # 4. DESCUBRIR SOLO LO NECESARIO
        #
        # Nuevas películas:
        #   -> necesitamos ID
        #
        # Preventas:
        #   -> actualizamos sus fechas
        #
        # TODO EN PARALELO.
        # ===================================================

        tareas_detalle = []

        indices_detalle = []


        for indice, pelicula in enumerate(
            peliculas
        ):

            if indice == 0:
                continue


            necesita_detalle = (
                not pelicula[
                    "movieId"
                ]
                or
                pelicula[
                    "estado"
                ] == "PREVENTA"
            )


            if necesita_detalle:

                indices_detalle.append(
                    indice
                )

                tareas_detalle.append(
                    obtener_detalle(
                        context,
                        pelicula["slug"],
                        semaforo_paginas
                    )
                )


        if tareas_detalle:

            print(
                "Actualizando IDs/preventas en paralelo..."
            )


            resultados_detalle = (
                await asyncio.gather(
                    *tareas_detalle
                )
            )


            for indice, detalle in zip(
                indices_detalle,
                resultados_detalle
            ):

                if not detalle:
                    continue


                peliculas[
                    indice
                ]["movieId"] = (
                    detalle[
                        "movieId"
                    ]
                )


                peliculas[
                    indice
                ][
                    "diasDisponibles"
                ] = (
                    detalle[
                        "diasDisponibles"
                    ]
                )


        # ===================================================
        # 5. PREPARAR TODAS LAS CONSULTAS
        # ===================================================

        tareas_api = []

        referencias = []


        for indice, pelicula in enumerate(
            peliculas
        ):

            if not pelicula.get(
                "movieId"
            ):

                print(
                    "Sin Movie ID:",
                    pelicula["titulo"]
                )

                continue


            if (
                pelicula["estado"]
                ==
                "PREVENTA"
            ):

                fechas = (
                    quitar_fechas_pasadas(
                        pelicula[
                            "diasDisponibles"
                        ]
                    )
                )

            else:

                fechas = (
                    fechas_cartelera()
                )


            pelicula[
                "_fechasConsulta"
            ] = fechas


            for fecha in fechas:

                referencias.append(
                    indice
                )

                tareas_api.append(
                    consultar_fecha(
                        context,
                        headers,
                        pelicula,
                        fecha,
                        semaforo_api
                    )
                )


        print()
        print(
            "Consultas API:",
            len(tareas_api)
        )

        print(
            f"Ejecutando hasta "
            f"{MAX_CONCURRENCIA} simultáneamente..."
        )


        # ===================================================
        # 6. TODAS LAS FECHAS EN PARALELO
        # ===================================================

        resultados_api = (
            await asyncio.gather(
                *tareas_api
            )
        )


        funciones_por_pelicula = {
            indice: []
            for indice
            in range(
                len(peliculas)
            )
        }


        for indice, funciones in zip(
            referencias,
            resultados_api
        ):

            funciones_por_pelicula[
                indice
            ].extend(
                funciones
            )


        # ===================================================
        # 7. GENERAR JSON
        # ===================================================

        resultado_peliculas = []


        for indice, pelicula in enumerate(
            peliculas
        ):

            funciones = (
                funciones_por_pelicula[
                    indice
                ]
            )


            funciones.sort(
                key=lambda x: (
                    x["fecha"],
                    x["hora"]
                )
            )


            dias_reales = sorted(
                set(
                    funcion["fecha"]

                    for funcion
                    in funciones
                )
            )


            resultado_peliculas.append({
                "titulo":
                    pelicula["titulo"],

                "slug":
                    pelicula["slug"],

                "movieId":
                    pelicula["movieId"],

                "estado":
                    pelicula["estado"],

                "duracionMinutos":
                    pelicula[
                        "duracionMinutos"
                    ],

                "clasificacion":
                    pelicula[
                        "clasificacion"
                    ],

                "cine":
                    CINE,

                "sede":
                    SEDE,

                "ciudad":
                    "Neiva",

                "confirmadaEnNeiva":
                    True,

                "diasDisponibles":
                    dias_reales,

                "funciones":
                    funciones
            })


            # Actualizar cache

            clave = pelicula[
                "clave"
            ]


            cache["peliculas"][
                clave
            ] = {
                "titulo":
                    pelicula["titulo"],

                "slug":
                    pelicula["slug"],

                "movieId":
                    pelicula["movieId"],

                "diasDisponibles":
                    (
                        pelicula[
                            "diasDisponibles"
                        ]
                        if pelicula[
                            "estado"
                        ] == "PREVENTA"
                        else dias_reales
                    )
            }


            print(
                pelicula["titulo"],
                "->",
                len(funciones),
                "funciones"
            )


        resultado = {
            "fuente":
                "Cinemark Colombia",

            "cine":
                CINE,

            "sede":
                SEDE,

            "ciudad":
                "Neiva",

            "actualizado":
                ahora_iso(),

            "peliculas":
                resultado_peliculas
        }


        guardar_json(
            ARCHIVO_SALIDA,
            resultado
        )


        guardar_json(
            ARCHIVO_CACHE,
            cache
        )


        await browser.close()


    # =======================================================
    # RESUMEN
    # =======================================================

    total_funciones = sum(
        len(
            pelicula[
                "funciones"
            ]
        )
        for pelicula
        in resultado_peliculas
    )


    duracion = (
        datetime.now()
        -
        inicio
    ).total_seconds()


    print()
    print(
        "=========================================="
    )
    print(
        "CINEMARK TURBO COMPLETADO"
    )
    print(
        "=========================================="
    )

    print(
        "Películas:",
        len(resultado_peliculas)
    )

    print(
        "Funciones:",
        total_funciones
    )

    print(
        f"Tiempo total: "
        f"{duracion:.1f} segundos"
    )

    print()
    print(
        "JSON:"
    )

    print(
        ARCHIVO_SALIDA
    )


if __name__ == "__main__":

    asyncio.run(
        main()
    )