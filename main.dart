import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'api.dart';
import 'top_list_screen.dart';

void main() => runApp(const TopListApp());

/// De app zelf. Houdt het gekozen thema (systeem/licht/donker) vast en onthoudt het.
class TopListApp extends StatefulWidget {
  const TopListApp({super.key});

  @override
  State<TopListApp> createState() => _TopListAppState();
}

class _TopListAppState extends State<TopListApp> {
  static const _prefsKey = 'theme_mode';
  ThemeMode _mode = ThemeMode.system;

  @override
  void initState() {
    super.initState();
    _loadMode();
  }

  Future<void> _loadMode() async {
    final prefs = await SharedPreferences.getInstance();
    setState(() => _mode = _decode(prefs.getString(_prefsKey)));
  }

  /// Wisselt door: systeem -> licht -> donker -> systeem, en bewaart de keuze.
  Future<void> _cycleMode() async {
    final next = switch (_mode) {
      ThemeMode.system => ThemeMode.light,
      ThemeMode.light => ThemeMode.dark,
      ThemeMode.dark => ThemeMode.system,
    };
    setState(() => _mode = next);
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString(_prefsKey, _encode(next));
  }

  static ThemeMode _decode(String? s) => switch (s) {
        'light' => ThemeMode.light,
        'dark' => ThemeMode.dark,
        _ => ThemeMode.system,
      };

  static String _encode(ThemeMode m) => switch (m) {
        ThemeMode.light => 'light',
        ThemeMode.dark => 'dark',
        ThemeMode.system => 'system',
      };

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Top-lijst',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        colorSchemeSeed: Colors.indigo,
        brightness: Brightness.light,
        useMaterial3: true,
      ),
      darkTheme: ThemeData(
        colorSchemeSeed: Colors.indigo,
        brightness: Brightness.dark,
        useMaterial3: true,
      ),
      themeMode: _mode,
      home: GenresScreen(themeMode: _mode, onCycleTheme: _cycleMode),
    );
  }
}

/// Startscherm: kies films of series en zie de genres uit je backend.
class GenresScreen extends StatefulWidget {
  final ThemeMode themeMode;
  final VoidCallback onCycleTheme;

  const GenresScreen({
    super.key,
    required this.themeMode,
    required this.onCycleTheme,
  });

  @override
  State<GenresScreen> createState() => _GenresScreenState();
}

class _GenresScreenState extends State<GenresScreen> {
  final ApiClient _api = ApiClient();

  String _mediaType = 'movie'; // 'movie' of 'tv'
  late Future<List<Genre>> _genresFuture;

  @override
  void initState() {
    super.initState();
    _genresFuture = _load();
  }

  Future<List<Genre>> _load() => _api.getGenres(mediaType: _mediaType);

  void _switchMediaType(String type) {
    if (type == _mediaType) return;
    setState(() {
      _mediaType = type;
      _genresFuture = _load();
    });
  }

  /// Opent de top-lijst. [genreId] null = alle genres.
  void _openTopList({int? genreId, required String genreName}) {
    Navigator.of(context).push(
      MaterialPageRoute(
        builder: (_) => TopListScreen(
          mediaType: _mediaType,
          genreId: genreId,
          genreName: genreName,
        ),
      ),
    );
  }

  /// Icoon dat de huidige themakeuze weergeeft.
  IconData get _themeIcon => switch (widget.themeMode) {
        ThemeMode.system => Icons.brightness_auto,
        ThemeMode.light => Icons.light_mode,
        ThemeMode.dark => Icons.dark_mode,
      };

  String get _themeTooltip => switch (widget.themeMode) {
        ThemeMode.system => 'Thema: systeem',
        ThemeMode.light => 'Thema: licht',
        ThemeMode.dark => 'Thema: donker',
      };

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('Kies een genre'),
        actions: [
          IconButton(
            tooltip: _themeTooltip,
            icon: Icon(_themeIcon),
            onPressed: widget.onCycleTheme,
          ),
        ],
      ),
      body: Column(
        children: [
          Padding(
            padding: const EdgeInsets.all(12),
            child: SegmentedButton<String>(
              segments: const [
                ButtonSegment(
                    value: 'movie',
                    label: Text('Films'),
                    icon: Icon(Icons.movie_outlined)),
                ButtonSegment(
                    value: 'tv',
                    label: Text('Series'),
                    icon: Icon(Icons.tv_outlined)),
              ],
              selected: {_mediaType},
              onSelectionChanged: (selection) =>
                  _switchMediaType(selection.first),
            ),
          ),
          // Altijd bovenaan: bekijk alles zonder genrefilter.
          ListTile(
            leading: const Icon(Icons.apps),
            title: Text(_mediaType == 'movie' ? 'Alle films' : 'Alle series'),
            subtitle: const Text('Zonder genrefilter'),
            trailing: const Icon(Icons.chevron_right),
            onTap: () => _openTopList(
              genreId: null,
              genreName: _mediaType == 'movie' ? 'films' : 'series',
            ),
          ),
          const Divider(height: 1),
          Expanded(
            child: FutureBuilder<List<Genre>>(
              future: _genresFuture,
              builder: (context, snapshot) {
                if (snapshot.connectionState == ConnectionState.waiting) {
                  return const Center(child: CircularProgressIndicator());
                }
                if (snapshot.hasError) {
                  return _ErrorView(
                    message: snapshot.error.toString(),
                    onRetry: () => setState(() {
                      _genresFuture = _load();
                    }),
                  );
                }
                final genres = snapshot.data ?? [];
                if (genres.isEmpty) {
                  return const Center(child: Text('Geen genres gevonden.'));
                }
                return ListView.separated(
                  itemCount: genres.length,
                  separatorBuilder: (_, __) => const Divider(height: 1),
                  itemBuilder: (context, i) {
                    final g = genres[i];
                    return ListTile(
                      title: Text(g.name),
                      trailing: const Icon(Icons.chevron_right),
                      onTap: () =>
                          _openTopList(genreId: g.id, genreName: g.name),
                    );
                  },
                );
              },
            ),
          ),
        ],
      ),
    );
  }
}

/// Nette foutweergave met een hint én een knop om opnieuw te proberen.
class _ErrorView extends StatelessWidget {
  final String message;
  final VoidCallback onRetry;
  const _ErrorView({required this.message, required this.onRetry});

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(24),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const Icon(Icons.wifi_off, size: 48),
            const SizedBox(height: 12),
            const Text(
              'Kan de backend niet bereiken',
              style: TextStyle(fontWeight: FontWeight.bold, fontSize: 16),
            ),
            const SizedBox(height: 8),
            const Text(
              'Zit je iPhone op hetzelfde wifi als de LXC? En staat de '
              'Info.plist-uitzondering voor HTTP erin?',
              textAlign: TextAlign.center,
            ),
            const SizedBox(height: 8),
            Text(
              message,
              textAlign: TextAlign.center,
              style: const TextStyle(fontSize: 12, color: Colors.grey),
            ),
            const SizedBox(height: 16),
            FilledButton.icon(
              onPressed: onRetry,
              icon: const Icon(Icons.refresh),
              label: const Text('Opnieuw proberen'),
            ),
          ],
        ),
      ),
    );
  }
}
