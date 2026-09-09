/* Offline benchmark bridge to the separately obtained Olaf C core.
 * Replaces only its file reader with an in-memory reader. The upstream
 * stream processor, FFT, fingerprints and database matcher are unchanged.
 * One runner must be used by one thread at a time.
 */
#include <stdlib.h>
#include <string.h>
#include "olaf_stream_processor.h"
#include "olaf_reader.h"

#ifdef _WIN32
#define EXPORT __declspec(dllexport)
#else
#define EXPORT __attribute__((visibility("default")))
#endif

typedef struct {
    const float *samples;
    size_t length;
} MemoryInput;

struct Olaf_Reader {
    const MemoryInput *input;
    Olaf_Config *config;
    size_t position;
};

Olaf_Reader *olaf_reader_new(Olaf_Config *config, const char *source) {
    Olaf_Reader *reader = calloc(1, sizeof(*reader));
    if (!reader) return NULL;
    reader->input = (const MemoryInput *)source;
    reader->config = config;
    return reader;
}

size_t olaf_reader_read(Olaf_Reader *reader, float *block) {
    size_t step = reader->config->audioStepSize;
    size_t overlap = reader->config->audioBlockSize - step;
    size_t remaining = reader->input->length - reader->position;
    size_t count = remaining < step ? remaining : step;
    memmove(block, block + step, overlap * sizeof(float));
    memcpy(block + overlap, reader->input->samples + reader->position, count * sizeof(float));
    memset(block + overlap + count, 0, (step - count) * sizeof(float));
    reader->position += count;
    return count;
}

size_t olaf_reader_total_samples_read(Olaf_Reader *reader) { return reader->position; }
void olaf_reader_destroy(Olaf_Reader *reader) { free(reader); }

EXPORT Olaf_Runner *bench_open(const char *database_directory) {
    Olaf_Config *config = olaf_config_default();
    free((void *)config->dbFolder);
    config->dbFolder = strdup(database_directory);
    return olaf_runner_new(OLAF_RUNNER_MODE_QUERY, config, NULL, NULL);
}

EXPORT size_t bench_query(Olaf_Runner *runner, const float *samples, size_t length,
                         Olaf_FP_Matcher_Result_Callback callback) {
    MemoryInput input = {samples, length};
    Olaf_Stream_Processor *processor = olaf_stream_processor_new(runner, (const char *)&input, "query.wav");
    if (!processor) return (size_t)-1;
    olaf_stream_processor_set_suppress_summary(processor, true);
    olaf_stream_processor_set_result_callback(processor, callback);
    olaf_stream_processor_process(processor);
    size_t total = olaf_stream_processor_total_fingerprints(processor);
    olaf_stream_processor_destroy(processor);
    return total;
}

EXPORT void bench_close(Olaf_Runner *runner) {
    Olaf_Config *config = runner->config;
    olaf_runner_destroy(runner);
    olaf_config_destroy(config);
}
