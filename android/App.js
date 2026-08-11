import { useState } from "react";
import {
  ActivityIndicator,
  Alert,
  Button,
  FlatList,
  Linking,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";
import { StatusBar } from "expo-status-bar";

const SERVER_URL =
  require("./app.json").expo.extra.serverBaseUrl ?? "http://192.168.1.100:8000";

export default function App() {
  const [url, setUrl] = useState("");
  const [loading, setLoading] = useState(false);
  const [info, setInfo] = useState(null);

  const fetchInfo = async () => {
    if (!url.trim()) return;
    setLoading(true);
    setInfo(null);
    try {
      const res = await fetch(
        `${SERVER_URL}/info?url=${encodeURIComponent(url.trim())}`
      );
      if (!res.ok) throw new Error(`Server error: ${res.status}`);
      setInfo(await res.json());
    } catch (e) {
      Alert.alert("Fetch failed", e.message);
    } finally {
      setLoading(false);
    }
  };

  const download = async (formatId) => {
    try {
      const res = await fetch(`${SERVER_URL}/download`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: url.trim(), format_id: formatId }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail ?? "Download failed");
      Alert.alert("Done", "File ready on server", [
        { text: "Open", onPress: () => Linking.openURL(`${SERVER_URL}${data.file_url}`) },
        { text: "OK" },
      ]);
    } catch (e) {
      Alert.alert("Download failed", e.message);
    }
  };

  return (
    <View style={styles.container}>
      <StatusBar style="light" />
      <Text style={styles.title}>Universal Downloader+</Text>
      <Text style={styles.subtitle}>Server: {SERVER_URL}</Text>

      <TextInput
        style={styles.input}
        placeholder="Paste video / playlist URL"
        placeholderTextColor="#666"
        value={url}
        onChangeText={setUrl}
        autoCapitalize="none"
        autoCorrect={false}
      />
      <Button title="Analyze" onPress={fetchInfo} disabled={loading} />

      {loading && <ActivityIndicator size="large" style={styles.spinner} />}

      {info && (
        <View style={styles.card}>
          <Text style={styles.title}>{info.title}</Text>
          <Text style={styles.meta}>
            {info.uploader ?? "unknown"} · {info.duration}s
          </Text>
          <FlatList
            data={info.formats ?? []}
            keyExtractor={(f, i) => `${f.format_id}-${i}`}
            renderItem={({ item }) => (
              <View style={styles.row}>
                <View style={styles.rowInfo}>
                  <Text style={styles.rowText}>
                    {item.resolution || "audio"} · {item.ext}
                  </Text>
                  <Text style={styles.rowMeta}>
                    {item.filesize ? `${Math.round(item.filesize / 1e6)} MB` : "size unknown"}
                  </Text>
                </View>
                <Button
                  title="Download"
                  onPress={() => download(item.format_id)}
                />
              </View>
            )}
          />
        </View>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: "#18181b",
    padding: 16,
    paddingTop: 64,
  },
  title: { color: "#fff", fontSize: 20, fontWeight: "700", marginBottom: 4 },
  subtitle: { color: "#34d399", fontSize: 12, marginBottom: 16 },
  input: {
    backgroundColor: "#27272a",
    color: "#fff",
    borderRadius: 8,
    padding: 12,
    marginBottom: 12,
  },
  spinner: { marginTop: 24 },
  card: { marginTop: 16, flex: 1 },
  meta: { color: "#a1a1aa", marginBottom: 12 },
  row: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    backgroundColor: "#27272a",
    borderRadius: 8,
    padding: 10,
    marginBottom: 8,
  },
  rowInfo: { flex: 1, marginRight: 8 },
  rowText: { color: "#fff", fontWeight: "600" },
  rowMeta: { color: "#a1a1aa", fontSize: 12 },
});
