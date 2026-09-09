// Local benchmark worker: binary PCM requests over stdin, JSON lines on stdout.
// No listening socket, decoder subprocess or per-query temporary audio file.
import be.panako.strategy.*;
import be.panako.strategy.panako.storage.PanakoStorageKV;
import be.panako.util.*;
import be.tarsos.dsp.io.*;
import java.io.*;
import java.util.*;

public class PanakoStreamServer {
    private static byte[] audio = new byte[0];

    public static void main(String[] args) throws Exception {
        Locale.setDefault(Locale.ROOT);
        PrintStream replies = new PrintStream(new FileOutputStream(FileDescriptor.out), true, "UTF-8");
        System.setOut(System.err);
        java.util.logging.LogManager.getLogManager().reset();
        for (Key key : Key.values()) Config.set(key, key.getDefaultValue());
        Config.set(Key.STRATEGY, "PANAKO");
        Config.set(Key.PANAKO_STORAGE, "LMDB");
        Config.set(Key.PANAKO_LMDB_FOLDER, args[0]);
        Config.set(Key.PANAKO_CACHE_TO_FILE, "FALSE");
        Config.set(Key.PANAKO_USE_CACHED_PRINTS, "FALSE");
        Config.set(Key.PANAKO_MIN_MATCH_DURATION, "1.5");
        PipedAudioStream.setDecoder(new PipeDecoder("/bin/bash", "-c", "unused", "unused", 32000) {
            public InputStream getDecodedStream(String path, int rate, double start, double duration) {
                if (rate != 16000) throw new IllegalArgumentException("Expected 16 kHz PCM16");
                int from = Math.min(audio.length, (int)(start * 32000));
                int length = duration <= 0 ? audio.length - from : Math.min(audio.length - from, (int)(duration * 32000));
                return new ByteArrayInputStream(audio, from, length);
            }
            public double getDuration(String path) { return audio.length / 32000.0; }
        });
        Strategy strategy = Strategy.getInstance();
        PanakoStorageKV.getInstance();
        DataInputStream input = new DataInputStream(new BufferedInputStream(System.in));
        replies.println("{\"ready\":true,\"storage\":\"LMDB\"}");
        try {
            while (true) {
                int operation;
                try { operation = input.readInt(); } catch (EOFException done) { break; }
                if (operation == 4) break;
                int id = input.readInt();
                int length = input.readInt();
                if (length < 0 || length > 200_000_000 || length % 2 != 0)
                    throw new IOException("Invalid PCM length");
                audio = new byte[length];
                input.readFully(audio);
                String path = id + ".pcm";
                // TarsosDSP validates path readability before invoking the
                // custom decoder. A reusable empty alias satisfies that check;
                // all audio still comes from the request buffer.
                File alias = new File(path);
                if (!alias.exists() && !alias.createNewFile()) throw new IOException("Cannot create alias");
                long start = System.nanoTime();
                if (operation == 0) {
                    replies.println("{\"pong\":true}");
                } else if (operation == 1 || operation == 3) {
                    if (operation == 1) strategy.store(path, path);
                    else strategy.delete(path);
                    replies.printf("{\"ok\":true,\"server_s\":%.9f}%n", (System.nanoTime()-start)/1e9);
                } else if (operation == 2) {
                    List<QueryResult> hits = new ArrayList<>();
                    strategy.query(path, 5, Collections.emptySet(), new QueryResultHandler() {
                        public void handleQueryResult(QueryResult r) { hits.add(r); }
                        public void handleEmptyResult(QueryResult r) {}
                    });
                    double elapsed = (System.nanoTime()-start)/1e9;
                    StringJoiner rows = new StringJoiner(",");
                    for (QueryResult r : hits) {
                        int sid = Integer.parseInt(r.refPath.replace(".pcm", ""));
                        rows.add(String.format(Locale.ROOT,
                            "{\"song_id\":%d,\"count\":%.0f,\"query_start\":%.9f,\"query_stop\":%.9f,\"ref_start\":%.9f,\"ref_stop\":%.9f,\"time_factor\":%.9f,\"frequency_factor\":%.9f,\"matched_seconds_fraction\":%.9f}",
                            sid,r.score,r.queryStart,r.queryStop,r.refStart,r.refStop,r.timeFactor,r.frequencyFactor,r.percentOfSecondsWithMatches));
                    }
                    replies.printf("{\"server_s\":%.9f,\"hits\":[%s]}%n", elapsed, rows.toString());
                } else throw new IOException("Unknown operation");
                audio = new byte[0];
            }
        } finally { PanakoStorageKV.getInstance().close(); }
    }
}
