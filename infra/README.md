# Infraestructura — etapa 3, pendiente

Espacio reservado para infraestructura como código. No hay recursos AWS creados.
Primero se valida el workflow local y la llamada simulada. El adaptador Lambda ejecutará
un turno y recuperará persistencia externa; no se empaquetará Agent Server completo en Lambda.
La persistencia DynamoDB requiere un adaptador explícito y pruebas de concurrencia/idempotencia.
