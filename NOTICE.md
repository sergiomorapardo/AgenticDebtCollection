# References

The module, node, and route layout was adapted from Sergio Mora's
[Alianza Team procurement agent](https://github.com/sergiomorapardo/alianzateam-claude-data-teams/tree/5d37d6e/src/agents/abastecimiento).
The original structural reference was a clean `main` checkout at `5d37d6e`.
The first commit contained only the initial `AGENTS.md`, copied from AdvancedGenAI.

The visual documentation adapts teaching patterns from these repositories:

| Reference | Pattern adapted here |
| --- | --- |
| Alianza Team: `S01_Pedidos_LangChain.ipynb`, `S03_Inventario_Tools.ipynb` | Short interaction sequences and explicit tool boundaries |
| Alianza Team: `S04_Documentos_RAG.ipynb` | Color distinction between local processing and paid APIs |
| AdvancedGenAI: `L4-RAGHistoryChat.ipynb`, `L10-chaining.ipynb` | Decision flows, persistent state, and graphs grounded in the implementation |

The collection code, synthetic fixtures, and diagrams were created for this project.
No credentials, customer data, course datasets, or procurement memory were copied.

## License compatibility

This repository is released under the [MIT License](LICENSE). AdvancedGenAI is MIT-licensed
by the same author. The Alianza Team references are private repositories maintained by the
author; only structure and teaching patterns were adapted from them.
