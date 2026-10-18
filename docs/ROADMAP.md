# Etapas

0. **Estructura completada:** organización modular, contratos y topología.
1. **Texto implementado y probado offline:** controles de contacto, identidad ficticia, motor de
   planes, parser offline, adaptador de extracción Haiku, persistencia SQLite, CLI y entrada Studio.
   La extracción Haiku requiere una prueba real con credenciales antes de declararla validada.
2. **Llamada simulada local, siguiente:** Gradio, micrófono, transcripción, voz y controles de
   iniciar/terminar. Primero turnos manuales; después pausas e interrupciones. Sin marcar números.
3. **Staging con simulador web:** autenticación, aislamiento, persistencia externa, infraestructura,
   secretos y observabilidad. Verificar concurrencia y recuperación en el entorno real.
4. **Telefonía de prueba:** adaptador Amazon Connect/Lex y validación de tiempos del canal;
   únicamente números autorizados expresamente. No forma parte de la simulación actual.
5. **Piloto controlado:** sistemas reales, políticas revisadas, supervisión y criterios de parada.

No presentar una fase como validada sin ejecutar sus criterios. El escenario de interrupción de
voz en eval/scenarios.json queda pendiente de la etapa 2. No se han consumido APIs pagadas.
