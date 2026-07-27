# Núcleo NKL

Blockchain propia con algoritmo memory-hard NKL-Argon, diseñada para minado 100% por CPU, sin necesidad de hardware especializado (ASIC/GPU). NKL es la criptomoneda nativa del ecosistema **Núcleo Laboratorios**.

## ¿Qué es Núcleo NKL?

NKL es una criptomoneda con blockchain independiente (no es un token sobre otra red), con reglas de consenso y validación propias. El algoritmo NKL-Argon usa la función memory-hard de Argon2 como base del hashing de minado, combinado con SHA-256 para la verificación final de cada bloque — esto obliga a reservar memoria RAM real durante el minado, encareciendo la construcción de hardware dedicado tipo ASIC.

## Ecosistema Núcleo Laboratorios

NKL no es un token aislado: es la moneda nativa que conecta un ecosistema de software real desarrollado por el mismo equipo:

- **[Núcleo Laboratorios](https://nucleolaboratorios.com)** — sitio central del ecosistema, con todos los productos.
- **[Núcleo CRM](https://nucleocrm.net)** — software de gestión inmobiliaria. El acceso se activa minando NKL, dándole utilidad real al token desde el día 1.
- **[Núcleo CERT](https://nucleocert.com)** — plataforma de certificación y firma digital con validez legal en Argentina (Ley 25.506), con anclaje de evidencia en blockchain.

## Enlaces

- **Web oficial de NKL:** https://nucleonkl.com
- **Pool de minado en vivo:** https://pool.nucleonkl.com
- **Explorador de bloques en vivo:** https://explorer.nucleonkl.com
- **Whitepapers técnicos:** carpeta [`/docs`](./docs) de este repositorio
  - Whitepaper I — Staking y NúcleoPoP
  - Whitepaper II — Tokenomics y Algoritmo NKL-Argon
  - Whitepaper III — Visión del Proyecto y Guía del Minero
  - Whitepaper IV — Pool de Distribución Nativo

## Estructura del repositorio

- `server.py` — nodo, pool y explorer (Flask)
- `nkl_constants.py` — parámetros del protocolo (supply, halving, dificultad)
- `NUCLEO_NKL_MINER/` — cliente de minado
- `docs/` — whitepapers técnicos (PDF)

## Tokenomics (resumen)

- **Supply total:** 100.000.000.000 NKL
- **Minable por la comunidad:** 70.000.000.000 NKL (70%)
- **Reserva del equipo fundador:** 30.000.000.000 NKL (30%), ya emitida desde el bloque 0 — no genera inflación adicional al liberarse
- **Halving:** reducción del 30% del reward cada 3 años, 10 halvings en 30 años
- Cronograma completo de liberación de la reserva y su justificación: ver sección "Tokenomics" en [nucleonkl.com](https://nucleonkl.com)

## Estado actual del proyecto

La red está en producción real, con bloques y mineros activos verificables en el explorer público. Actualmente opera con un único nodo validador (etapa centralizada de la hoja de ruta); la migración a validación distribuida está documentada en el whitepaper de "Pool de Distribución Nativo".

## Reconocimiento

Aplicación enviada y aceptada al **Entrepreneurship World Cup (EWC) 2026**, compitiendo junto a emprendedores de 190+ países.

## Comunidad

- **X / Twitter:** https://x.com/NucleoNKL
- **Telegram:** https://t.me/nucleonkl
- **Discord:** https://discord.gg/86Quc6Rutp
- **Instagram:** https://www.instagram.com/minar_nucleonkl/
- **Facebook:** https://www.facebook.com/profile.php?id=61564701027741
- **TikTok:** https://www.tiktok.com/@nucleonkl

## Autor

Jose Luis — Fundador y desarrollador. Moreno, Buenos Aires, Argentina.
