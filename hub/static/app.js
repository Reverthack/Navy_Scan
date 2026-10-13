(() => {
  "use strict";

  const REFRESH_MS = 5000;
  const SHIP_WINDOW_MS = 15 * 60 * 1000;
  const SEV_COLOR = { critique: "#ef4444", alerte: "#f59e0b", info: "#38bdf8" };
  const SEV_LABEL = { critique: "Critique", alerte: "Alerte", info: "Info" };
  const KIND_LABEL = {
    intrusion_sans_ais: "Intrusion sans AIS",
    navire_identifie: "Navire identifié",
  };
  const FLAG_LABEL = {
    vitesse_elevee: "Vitesse élevée",
    humain: "Humain détecté",
  };
  const CLASS_LABEL = { humain: "Humain", embarcation: "Embarcation" };
  const CAM_KIND_LABEL = {
    thermique_panoramique: "Thermique panoramique 360°",
    thermique_ptz: "Thermique PTZ",
    bullet_thermique: "Bullet thermique",
    visible_ptz: "Visible PTZ",
  };
  const CAM_STATUS = {
    en_ligne: { label: "En ligne", cls: "st-online" },
    hors_ligne: { label: "Hors ligne", cls: "st-offline" },
    silencieuse: { label: "Silencieuse", cls: "st-idle" },
    desactivee: { label: "Désactivée", cls: "st-off" },
  };

  const $ = (id) => document.getElementById(id);
  const fmtTime = new Intl.DateTimeFormat("fr-FR", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  const fmtDate = new Intl.DateTimeFormat("fr-FR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });

  // ---------- Carte ----------
  const map = L.map("map", { zoomControl: false }).setView([48.39, -4.49], 14);
  L.control.zoom({ position: "bottomright" }).addTo(map);
  // Fond de carte configurable (config.json > map). Par défaut : OpenStreetMap, sans clé.
  const DEFAULT_MAP = {
    tile_url: "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    attribution: "&copy; OpenStreetMap contributors",
    max_zoom: 19,
    dark: true,
  };
  let tileLayer = null;
  let tilesSeen = false;

  function setMapNotice(text) {
    const notice = $("mapNotice");
    notice.textContent = text || "";
    notice.hidden = !text;
  }

  function applyMapConfig(cfg) {
    const c = { ...DEFAULT_MAP, ...(cfg || {}) };
    if (tileLayer) map.removeLayer(tileLayer);
    tilesSeen = false;
    tileLayer = L.tileLayer(c.tile_url, { maxZoom: c.max_zoom, attribution: c.attribution }).addTo(map);
    // Si aucune tuile ne charge, on le dit au lieu de laisser une carte vide.
    tileLayer.on("tileload", () => { tilesSeen = true; setMapNotice(""); });
    tileLayer.on("tileerror", () => {
      if (tilesSeen) return;
      setMapNotice(c.provider === "carto"
        ? "Fond de carte CARTO indisponible : vérifier MAP_TILE_KEY dans .env (clé valide ?) et la connexion Internet."
        : "Fond de carte indisponible : vérifier la connexion Internet.");
    });
    $("map").classList.toggle("map-dark", Boolean(c.dark));
  }
  applyMapConfig(null);

  const zoneLayer = L.layerGroup().addTo(map);
  const siteLayer = L.layerGroup().addTo(map);
  const cameraLayer = L.layerGroup().addTo(map);
  const vesselLayer = L.layerGroup().addTo(map);
  const alarmLayer = L.layerGroup().addTo(map);

  // ---------- État ----------
  let alarms = [];
  let vessels = [];
  let ptzCommands = [];
  let cameras = [];
  let aisStatus = null;
  let sites = null;
  let fitted = false;

  // ---------- Utilitaires ----------
  async function api(path, options = {}) {
    const res = await fetch(path, options);
    if (!res.ok) throw new Error(`${res.status} ${path}`);
    return res.json();
  }

  // Écriture : renvoie { ok, status, data } sans lever d'erreur, pour afficher le détail serveur.
  async function apiWrite(path, method, key, body) {
    const headers = { "X-API-Key": key };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const res = await fetch(path, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    let data = null;
    try { data = await res.json(); } catch (_) { /* pas de corps */ }
    return { ok: res.ok, status: res.status, data };
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function ageMs(iso) {
    return Date.now() - Date.parse(iso);
  }

  function formatAge(iso) {
    const s = Math.max(0, Math.round(ageMs(iso) / 1000));
    if (s < 60) return `il y a ${s} s`;
    if (s < 3600) return `il y a ${Math.round(s / 60)} min`;
    return `il y a ${Math.round(s / 3600)} h`;
  }

  function setLive(ok) {
    $("liveDot").className = `dot ${ok ? "live" : "down"}`;
    $("liveText").textContent = ok ? "Temps réel" : "Hors ligne";
  }

  function siteName(siteId) {
    return sites && sites[siteId] ? sites[siteId].name : siteId;
  }

  function zoneNames(cam) {
    const site = sites && sites[cam.site_id];
    if (!site) return "";
    if (!cam.zone_ids.length) return "toutes les zones";
    return cam.zone_ids
      .map((id) => (site.zones.find((z) => z.id === id) || { name: id }).name)
      .join(", ");
  }

  // ---------- Carte ----------
  function drawSites() {
    zoneLayer.clearLayers();
    siteLayer.clearLayers();
    Object.values(sites).forEach((site) => {
      L.marker([site.lat, site.lon], {
        icon: L.divIcon({ className: "site-icon", iconSize: [18, 18], iconAnchor: [9, 9] }),
        title: site.name,
      }).bindTooltip(site.name).addTo(siteLayer);

      site.zones.forEach((zone) => {
        const restricted = Boolean(zone.restricted);
        L.polygon(zone.polygon, {
          color: restricted ? SEV_COLOR.critique : SEV_COLOR.alerte,
          weight: 2,
          dashArray: "6 6",
          fillOpacity: restricted ? 0.14 : 0.1,
        })
          .bindTooltip(`${zone.name}${restricted ? " · zone restreinte" : ""}`)
          .addTo(zoneLayer);
      });
    });

    if (!fitted && zoneLayer.getLayers().length) {
      map.fitBounds(L.featureGroup(zoneLayer.getLayers()).getBounds().pad(0.4));
      fitted = true;
    }
  }

  function drawCameras() {
    cameraLayer.clearLayers();
    cameras.forEach((cam) => {
      const st = CAM_STATUS[cam.status] || CAM_STATUS.silencieuse;
      const online = cam.status === "en_ligne";
      L.marker([cam.lat, cam.lon], {
        icon: L.divIcon({
          className: `cam-icon ${online ? "cam-on" : "cam-off"}`,
          iconSize: [16, 16],
          iconAnchor: [8, 8],
        }),
        title: cam.name,
      })
        .bindTooltip(`${cam.name} · ${st.label} · ${zoneNames(cam)}`)
        .on("click", () => openCameraDialog(cam))
        .addTo(cameraLayer);
    });
  }

  function drawVessels() {
    vesselLayer.clearLayers();
    vessels.forEach((v) => {
      const recent = ageMs(v.last_seen) < SHIP_WINDOW_MS;
      L.circleMarker([v.lat, v.lon], {
        radius: 6,
        color: "#0b1120",
        weight: 1,
        fillColor: recent ? "#38bdf8" : "#64748b",
        fillOpacity: 0.95,
      })
        .bindTooltip(`${v.name || "Navire inconnu"} · MMSI ${v.mmsi} · ${formatAge(v.last_seen)}`)
        .addTo(vesselLayer);
    });
  }

  function drawAlarms() {
    alarmLayer.clearLayers();
    alarms
      .filter((a) => !a.acked)
      .forEach((a) => {
        L.circleMarker([a.lat, a.lon], {
          radius: 12,
          color: SEV_COLOR[a.severity] || SEV_COLOR.info,
          weight: 2,
          fillOpacity: 0.2,
          className: a.severity === "critique" ? "pulse" : "",
        })
          .bindTooltip(`${KIND_LABEL[a.kind] || a.kind} · ${a.zone}`)
          .addTo(alarmLayer);
      });
  }

  // ---------- Indicateurs ----------
  function renderKpis() {
    const openCrit = alarms.filter((a) => a.severity === "critique" && !a.acked).length;
    const openAlert = alarms.filter((a) => a.severity === "alerte" && !a.acked).length;
    const ships = vessels.filter((v) => ageMs(v.last_seen) < SHIP_WINDOW_MS).length;
    const online = cameras.filter((c) => c.status === "en_ligne").length;

    $("kCrit").textContent = openCrit;
    $("kAlert").textContent = openAlert;
    $("kShips").textContent = ships;
    $("kCams").textContent = `${online} / ${cameras.length}`;
  }

  // ---------- Caméras (liste) ----------
  function renderCameras() {
    const list = $("cameras");
    list.replaceChildren();
    if (!cameras.length) {
      list.append(el("li", "empty", "Aucune caméra déclarée"));
      return;
    }
    cameras.forEach((cam) => {
      const st = CAM_STATUS[cam.status] || CAM_STATUS.silencieuse;
      const li = el("li", "cam-item");
      const head = el("div", "cam-head");
      head.append(el("strong", null, cam.name), el("span", `cam-status ${st.cls}`, st.label));

      const meta = [cam.id, siteName(cam.site_id), CAM_KIND_LABEL[cam.kind] || cam.kind];
      const body = el("div", "cam-meta", meta.join(" · "));
      const zones = el("div", "cam-meta", `Zones : ${zoneNames(cam)}`);

      const chips = el("div", "chips");
      if (cam.ptz) chips.append(el("span", "chip chip-warn", "PTZ"));
      if (cam.last_event_at) {
        chips.append(el("span", "chip", `dernier signal ${formatAge(cam.last_event_at)}`));
      }

      li.append(head, body, zones, chips);
      li.addEventListener("click", () => {
        openCameraDialog(cam);
        map.flyTo([cam.lat, cam.lon], 16, { duration: 0.8 });
      });
      list.append(li);
    });
  }

  // ---------- Dialogue caméra ----------
  let editingId = null;

  function populateSites(selected) {
    const select = $("cameraForm").elements.site_id;
    select.replaceChildren();
    Object.entries(sites).forEach(([id, site]) => {
      const opt = el("option", null, site.name);
      opt.value = id;
      if (id === selected) opt.selected = true;
      select.append(opt);
    });
  }

  function renderZoneChecks(siteId, checked = []) {
    const box = $("zoneChecks");
    box.replaceChildren();
    const site = sites[siteId];
    if (!site || !site.zones.length) {
      box.append(el("span", "muted", "Aucune zone définie pour ce site."));
      return;
    }
    site.zones.forEach((zone) => {
      const label = el("label", "check");
      const input = el("input");
      input.type = "checkbox";
      input.name = "zone_ids";
      input.value = zone.id;
      input.checked = checked.includes(zone.id);
      label.append(input, document.createTextNode(` ${zone.name}${zone.restricted ? " (restreinte)" : ""}`));
      box.append(label);
    });
  }

  function openCameraDialog(cam = null) {
    editingId = cam ? cam.id : null;
    const form = $("cameraForm");
    form.reset();
    $("cameraError").textContent = "";
    $("cameraTitle").textContent = cam ? `Modifier ${cam.name}` : "Nouvelle caméra";
    $("cameraDelete").hidden = !cam;

    const first = Object.keys(sites)[0];
    const siteId = cam ? cam.site_id : first;
    populateSites(siteId);

    form.elements.id.value = cam ? cam.id : "";
    form.elements.id.readOnly = Boolean(cam);
    form.elements.name.value = cam ? cam.name : "";
    form.elements.lat.value = cam ? cam.lat : sites[siteId].lat;
    form.elements.lon.value = cam ? cam.lon : sites[siteId].lon;
    form.elements.kind.value = cam ? cam.kind : "thermique_panoramique";
    form.elements.ptz.checked = cam ? cam.ptz : false;
    form.elements.enabled.checked = cam ? cam.enabled : true;
    renderZoneChecks(siteId, cam ? cam.zone_ids : []);

    $("cameraDialog").showModal();
    form.elements.name.focus();
  }

  function collectCamera() {
    const form = $("cameraForm");
    return {
      id: form.elements.id.value.trim(),
      site_id: form.elements.site_id.value,
      name: form.elements.name.value.trim(),
      lat: Number(form.elements.lat.value),
      lon: Number(form.elements.lon.value),
      kind: form.elements.kind.value,
      zone_ids: [...form.querySelectorAll('input[name="zone_ids"]:checked')].map((i) => i.value),
      ptz: form.elements.ptz.checked,
      enabled: form.elements.enabled.checked,
    };
  }

  // ---------- Clé API ----------
  function askApiKey(reason) {
    return new Promise((resolve) => {
      const dialog = $("keyDialog");
      $("keyInput").value = "";
      $("keyReason").textContent = reason || "Cette action modifie les données du hub. Saisir la clé du hub.";
      dialog.onclose = () => resolve(dialog.returnValue === "ok" ? $("keyInput").value : null);
      dialog.showModal();
      $("keyInput").focus();
    });
  }

  async function getKey(reason) {
    let key = sessionStorage.getItem("cs_api_key");
    if (!key) {
      key = await askApiKey(reason);
      if (!key) return null;
      sessionStorage.setItem("cs_api_key", key);
    }
    return key;
  }

  function forgetKey() {
    sessionStorage.removeItem("cs_api_key");
  }

  async function acknowledge(id) {
    const key = await getKey("Acquitter cette alarme : saisir la clé du hub.");
    if (!key) return;
    const res = await apiWrite(`/api/alarms/${id}/ack`, "POST", key);
    if (res.status === 401) {
      forgetKey();
      alert("Clé API refusée. Réessayer.");
      return;
    }
    await refresh();
  }

  async function saveCamera(event) {
    event.preventDefault();
    const body = collectCamera();
    const key = await getKey("Enregistrer la caméra : saisir la clé du hub.");
    if (!key) return;

    const res = editingId
      ? await apiWrite(`/api/cameras/${encodeURIComponent(editingId)}`, "PUT", key, body)
      : await apiWrite("/api/cameras", "POST", key, body);

    if (res.status === 401) {
      forgetKey();
      $("cameraError").textContent = "Clé API refusée.";
      return;
    }
    if (!res.ok) {
      const detail = res.data && res.data.detail;
      $("cameraError").textContent = Array.isArray(detail)
        ? detail.map((d) => d.msg).join(" ; ")
        : (detail || `Erreur ${res.status}`);
      return;
    }
    $("cameraDialog").close();
    await refresh();
  }

  async function deleteCamera() {
    if (!editingId) return;
    if (!window.confirm(`Supprimer la caméra ${editingId} ? Ses détections seront ensuite refusées.`)) return;
    const key = await getKey("Supprimer la caméra : saisir la clé du hub.");
    if (!key) return;
    const res = await apiWrite(`/api/cameras/${encodeURIComponent(editingId)}`, "DELETE", key);
    if (res.status === 401) {
      forgetKey();
      $("cameraError").textContent = "Clé API refusée.";
      return;
    }
    $("cameraDialog").close();
    await refresh();
  }

  // ---------- Alarmes (liste) ----------
  function chipsFor(a) {
    const row = el("div", "chips");
    if (a.classe) row.append(el("span", "chip", CLASS_LABEL[a.classe] || a.classe));
    (a.flags || []).forEach((f) => {
      const warn = f === "humain" ? "chip chip-crit" : "chip chip-warn";
      row.append(el("span", warn, FLAG_LABEL[f] || f));
    });
    if (a.vitesse_mps != null) row.append(el("span", "chip", `${a.vitesse_mps} m/s`));
    return row;
  }

  function renderAlarms() {
    const list = $("alarms");
    const onlyOpen = $("filter").value === "open";
    const items = alarms.filter((a) => !onlyOpen || !a.acked);
    list.replaceChildren();

    if (!items.length) {
      list.append(el("li", "empty", "Aucune alarme"));
      return;
    }

    items.slice(0, 200).forEach((a) => {
      const li = el("li", `item sev-${a.severity}${a.acked ? " acked" : ""}`);
      const body = el("div", `item-body${a.has_snapshot ? " with-thumb" : ""}`);

      if (a.has_snapshot) {
        const img = el("img", "thumb");
        img.loading = "lazy";
        img.alt = "Vignette de l'alarme";
        img.src = `/api/alarms/${a.id}/snapshot`;
        body.append(img);
      }

      const main = el("div");
      const head = el("div", "item-head");
      head.append(
        el("span", `badge sev-${a.severity}`, SEV_LABEL[a.severity] || a.severity),
        el("strong", null, KIND_LABEL[a.kind] || a.kind),
      );
      const details = [a.zone, `cam ${a.camera_id}`];
      if (a.track_id != null) details.push(`piste #${a.track_id}`);
      details.push(fmtDate.format(new Date(a.created_at)));
      main.append(head, el("div", "item-meta", details.join(" · ")));

      if (a.kind === "navire_identifie") {
        main.append(el("div", "item-meta",
          `${a.vessel_name || "Inconnu"} · MMSI ${a.mmsi} · ${a.distance_m} m`));
      }
      main.append(chipsFor(a));

      const actions = el("div", "item-actions");
      if (a.acked) {
        actions.append(el("span", "acked-tag", "✓ Acquittée"));
      } else {
        const btn = el("button", "btn", "Acquitter");
        btn.addEventListener("click", (e) => {
          e.stopPropagation();
          acknowledge(a.id);
        });
        actions.append(btn);
      }
      main.append(actions);
      body.append(main);
      li.append(body);

      li.addEventListener("click", () => map.flyTo([a.lat, a.lon], 16, { duration: 0.8 }));
      list.append(li);
    });
  }

  // ---------- Navires ----------
  function renderAisStatus() {
    const box = $("aisStatus");
    const s = aisStatus;
    let cls = "ais-status";
    let text;
    if (!s || s.state === "desactive") {
      text = "Écoute AIS désactivée (ais.enabled = false dans config.json, ou CS_AIS=0).";
    } else if (s.state === "erreur") {
      cls += " crit";
      text = `AIS indisponible : port UDP ${s.udp_port} inaccessible (${s.error}). `
        + "Fermer le programme qui l'utilise, ou changer ais.udp_port dans config.json.";
    } else if (s.messages > 0) {
      text = `Écoute UDP ${s.udp_port} · ${s.messages} message(s) reçu(s), dernier message ${formatAge(s.last_message_at)}.`;
    } else {
      cls += " warn";
      text = `Écoute UDP ${s.udp_port} active, mais aucun message reçu. `
        + "Sans antenne AIS, lancer « py scripts\\cs.py simulate-nav ».";
    }
    box.className = cls;
    box.textContent = text;
  }

  function renderVessels() {
    const list = $("vessels");
    const query = $("search").value.trim().toLowerCase();
    const items = vessels
      .filter((v) => !query ||
        (v.name || "").toLowerCase().includes(query) ||
        String(v.mmsi).includes(query))
      .sort((a, b) => Date.parse(b.last_seen) - Date.parse(a.last_seen));

    list.replaceChildren();
    if (!items.length) {
      list.append(el("li", "empty", vessels.length ? "Aucun résultat" : "Aucun navire reçu"));
      return;
    }

    items.forEach((v) => {
      const recent = ageMs(v.last_seen) < SHIP_WINDOW_MS;
      const li = el("li", "vessel");
      const left = el("div");
      left.append(
        el("div", "vessel-name", v.name || "Navire inconnu"),
        el("div", "vessel-mmsi", `MMSI ${v.mmsi}${v.ship_type != null ? ` · type AIS ${v.ship_type}` : ""}`),
      );
      li.append(left, el("span", `vessel-age${recent ? " recent" : ""}`, formatAge(v.last_seen)));
      li.addEventListener("click", () => map.flyTo([v.lat, v.lon], 15, { duration: 0.8 }));
      list.append(li);
    });
  }

  // ---------- PTZ ----------
  function renderPtz() {
    const list = $("ptz");
    list.replaceChildren();
    if (!ptzCommands.length) {
      list.append(el("li", "empty", "Aucune commande"));
      return;
    }
    ptzCommands.slice(0, 20).forEach((c) => {
      const li = el("li", "ptz-item");
      li.append(
        el("strong", null, `Cap caméra ${c.camera_id}`),
        el("div", null, `Alarme #${c.alarm_id ?? "—"} · ${Number(c.lat).toFixed(5)}, ${Number(c.lon).toFixed(5)}`),
        el("div", null, `${fmtDate.format(new Date(c.sent_at))} · backend ${c.backend}`),
      );
      list.append(li);
    });
  }

  // ---------- Rafraîchissement ----------
  async function refresh() {
    try {
      if (!sites) {
        const [siteData, status] = await Promise.all([api("/api/sites"), api("/api/status")]);
        sites = siteData;
        const first = Object.values(sites)[0];
        if (first) $("siteName").textContent = first.name;
        $("version").textContent = `v${status.version}`;
        applyMapConfig(status.map);
        drawSites();
      }
      [alarms, vessels, ptzCommands, cameras, aisStatus] = await Promise.all([
        api("/api/alarms?limit=200"),
        api("/api/vessels"),
        api("/api/ptz/commands"),
        api("/api/cameras"),
        api("/api/status").then((s) => s.ais),
      ]);
      setLive(true);
    } catch (err) {
      setLive(false);
      return;
    }
    renderKpis();
    renderAlarms();
    renderCameras();
    renderAisStatus();
    renderVessels();
    renderPtz();
    drawCameras();
    drawVessels();
    drawAlarms();
  }

  function tickClock() {
    $("clock").textContent = fmtTime.format(new Date());
  }

  // ---------- Événements ----------
  $("filter").addEventListener("change", renderAlarms);
  $("search").addEventListener("input", renderVessels);
  $("addCamera").addEventListener("click", () => openCameraDialog(null));
  $("cameraCancel").addEventListener("click", () => $("cameraDialog").close());
  $("cameraDelete").addEventListener("click", deleteCamera);
  $("cameraForm").addEventListener("submit", saveCamera);
  $("cameraForm").elements.site_id.addEventListener("change", (e) => {
    const site = sites[e.target.value];
    $("cameraForm").elements.lat.value = site.lat;
    $("cameraForm").elements.lon.value = site.lon;
    renderZoneChecks(e.target.value);
  });

  tickClock();
  setInterval(tickClock, 1000);
  refresh();
  setInterval(refresh, REFRESH_MS);
})();
