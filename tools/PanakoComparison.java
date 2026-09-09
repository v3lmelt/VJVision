// Offline bridge for Panako 2.1. Input files are predecoded 16 kHz mono PCM16.
// Keeps one JVM and an in-memory index; never invokes an audio decoder process.
import be.panako.strategy.*;
import be.panako.util.*;
import be.tarsos.dsp.io.*;
import java.io.*;
import java.nio.file.*;
import java.util.*;

public class PanakoComparison {
    public static void main(String[] args) throws Exception {
        Locale.setDefault(Locale.ROOT);
        java.util.logging.LogManager.getLogManager().reset();
        Config.set(Key.STRATEGY, "PANAKO");
        Config.set(Key.PANAKO_STORAGE, "MEMORY");
        Config.set(Key.PANAKO_CACHE_TO_FILE, "FALSE");
        Config.set(Key.PANAKO_USE_CACHED_PRINTS, "FALSE");
        if (args.length > 1) Config.set(Key.PANAKO_MIN_MATCH_DURATION, args[1]);
        PipedAudioStream.setDecoder(new PipeDecoder("/bin/bash", "-c", "unused", "unused", 32000) {
            public InputStream getDecodedStream(String path, int rate, double start, double duration) {
                if (rate != 16000) throw new IllegalArgumentException("Expected 16 kHz");
                try {
                    byte[] bytes = Files.readAllBytes(Paths.get(path));
                    int from = Math.min(bytes.length, (int)(start * 32000));
                    int length = duration <= 0 ? bytes.length - from : Math.min(bytes.length - from, (int)(duration * 32000));
                    return new ByteArrayInputStream(bytes, from, length);
                } catch (IOException e) { throw new UncheckedIOException(e); }
            }
            public double getDuration(String path) {
                try { return Files.size(Paths.get(path)) / 32000.0; }
                catch (IOException e) { throw new UncheckedIOException(e); }
            }
        });
        Strategy strategy = Strategy.getInstance();
        Set<Integer> avoid = new HashSet<>();
        Path manifest = Paths.get(args[0]).toAbsolutePath();
        for (String line : Files.readAllLines(manifest)) {
            String[] parts = line.split("\t");
            String path = manifest.getParent().resolve(parts[1]).toString();
            if (parts[0].equals("S")) { strategy.store(path, parts[1]); continue; }
            // Panako's memory storage implements deletion as a no-op. Its
            // query exclusion set filters these IDs before ranking results.
            if (parts[0].equals("D")) { avoid.add(Integer.parseInt(strategy.resolve(path))); continue; }
            List<QueryResult> hits = new ArrayList<>();
            long start = System.nanoTime();
            strategy.query(path, 5, avoid, new QueryResultHandler() {
                public void handleQueryResult(QueryResult r) { hits.add(r); }
                public void handleEmptyResult(QueryResult r) {}
            });
            double elapsed = (System.nanoTime() - start) / 1e9;
            hits.sort((a, b) -> Double.compare(b.score, a.score));
            QueryResult top = hits.isEmpty() ? null : hits.get(0);
            System.out.printf("RESULT\t%s\t%s\t%.6f\t%.1f\t%.4f\t%.4f\t%.1f\t%.4f%n", parts[1],
                top == null ? "none" : Paths.get(top.refPath).getFileName().toString(), elapsed,
                top == null ? 0 : top.score, top == null ? 0 : top.timeFactor,
                top == null ? 0 : top.frequencyFactor,
                hits.size() < 2 ? 0 : hits.get(1).score,
                top == null ? 0 : top.queryStop - top.queryStart);
        }
    }
}
