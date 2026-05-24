// Photoshop Link host JSX（Photoshop ExtendScript）
// CEP パネルから呼ばれる関数群。Premiere Link 互換の launcher 三関数だけ提供。
// Photoshop 固有の操作関数はこの後ユーザーと一緒に追加していく。

function getJsxFiles(folderPath) {
    try {
        var fdr = new Folder(folderPath);
        if (!fdr.exists) return "";
        var fls = fdr.getFiles("*.jsx");
        var ar = [];
        for (var i = 0; i < fls.length; i++) {
            ar.push(decodeURI(fls[i].name));
        }
        return ar.join(",");
    } catch (e) {
        return "";
    }
}

function callJsxFile(filePath) {
    $.level = 0;
    try {
        $.evalFile(new File(filePath));
        return "ok";
    } catch (e) {
        return "error: " + e.message;
    }
}

function pickFolder(promptText) {
    var fdr = Folder.selectDialog(promptText || "Select script folder");
    return fdr ? fdr.fsName : "";
}
