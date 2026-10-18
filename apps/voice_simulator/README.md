# Simulador de llamada — etapa 2, pendiente

Interfaz prevista con Gradio: seleccionar cliente ficticio, iniciar, silenciar y terminar;
micrófono → transcripción → servidor local LangGraph → texto validado → TTS → altavoz.
Mostrar transcripción, latencia, etapa, oferta y acuerdo guardado.
Primero turnos manuales; después VAD e interrupciones con streaming/FastRTC si la evaluación
lo justifica. Studio y el simulador compartirán servidor y thread_id. No hay telefonía.
No se instala Gradio ni un proveedor de voz hasta implementar esta etapa.
