import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

import UM 1.5 as UM
import Cura 1.0 as Cura

// "manager" is injected from Python (MakerBotMiniManager).
UM.Dialog
{
    id: dialog
    title: "MakerBot Replicator Mini"

    minimumWidth: 440 * screenScaleFactor
    minimumHeight: 280 * screenScaleFactor
    width: minimumWidth
    height: minimumHeight

    ColumnLayout
    {
        anchors.fill: parent
        spacing: 10 * screenScaleFactor

        Label
        {
            Layout.fillWidth: true
            wrapMode: Text.WordWrap
            color: UM.Theme.getColor("text")
            text: "Enter the printer's IP address (shown in its network settings), then press Pair and accept the connection on the printer."
        }

        RowLayout
        {
            Layout.fillWidth: true
            spacing: 8 * screenScaleFactor

            TextField
            {
                id: ipField
                Layout.fillWidth: true
                placeholderText: "192.168.1.50"
                text: manager.ip
                enabled: !manager.busy
                onTextEdited: manager.setIp(text)
                onEditingFinished: manager.setIp(text)
            }

            Cura.SecondaryButton
            {
                text: "Scan"
                enabled: !manager.busy
                onClicked: manager.scan()
            }
        }

        Label
        {
            Layout.fillWidth: true
            Layout.fillHeight: true
            wrapMode: Text.WordWrap
            color: UM.Theme.getColor("text")
            text: manager.status
        }

        RowLayout
        {
            Layout.fillWidth: true
            spacing: 8 * screenScaleFactor

            Cura.PrimaryButton
            {
                text: manager.paired ? "Pair again" : "Pair"
                enabled: !manager.busy && ipField.text.length > 0
                onClicked:
                {
                    manager.setIp(ipField.text)
                    manager.pair()
                }
            }

            Cura.SecondaryButton
            {
                text: "Cancel pairing"
                visible: manager.busy
                onClicked: manager.cancelPairing()
            }

            Cura.SecondaryButton
            {
                text: "Forget printer"
                visible: manager.paired && !manager.busy
                onClicked: manager.forget()
            }
        }
    }

    rightButtons: [
        Cura.SecondaryButton
        {
            text: "Close"
            onClicked: dialog.reject()
        }
    ]
}
