# Data

`data/report_task/` contains the small report-repair task and Docker inputs. `data/fixtures/checkpoint_awareness_v3/` contains a synthetic source-only packet, matching schema and scripted annotation response. It contains no participant return or model-run claim. Other tests construct bounded fixtures in temporary directories.

Versioned prompts, rubrics and output schemas live beside their checkpoint study under `src/emergence/checkpoints/configs/`. A schema is not a dataset, accepted annotation or completed run. Different instrument versions remain separate files. The broad worker takes explicit input records and does not implicitly choose a source registry.

External task catalogues, model weights, training data, checkpoint releases and experiment outputs are supplied separately. This checkout does not claim to distribute a complete empirical dataset or every source required by every historical campaign. Follow the original source's licensing and access conditions when supplying an input. Record an immutable revision, byte size and checksum, along with permitted redistribution status and experimental-unit identifiers.

Keep original source bytes and record mappings explicitly. Do not silently rewrite a frozen manifest when creating a derived public descriptor. A different path layout or corrected parser is a new implementation identity even when its intended calculation is unchanged. Outputs should go to caller-selected locations and should retain original assignment IDs, protocol identifiers and missingness states.
