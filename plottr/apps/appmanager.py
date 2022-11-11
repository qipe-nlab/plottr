"""This module contains tools for launching plottr apps and managing currently
running apps.

An `app` as used in plottr is defined as a function that returns a
:class:`plottr.node.node.Flowchart` and :class:`plottr.gui.widgets.PlotWindow`.
This function will receive a tuple with the arguments it needs and it is the job of the function to unpack them.

The role of the :class:`.AppManager` is to launch, manage, and communicate with
app processes. An app can be launched using the launchApp function.

.. note::
    Make sure all the arguments your app needs are being passed and are correct. Any error while trying to open the app
    will result in the app not opening without an error warning.
"""

import zmq
from pathlib import Path
from typing import Dict, Union, Any, Callable, Tuple, Optional

from traceback import print_exception
from plottr import QtCore, QtWidgets, QtGui, Flowchart, Signal, Slot, log, qtapp, qtsleep, plottrPath
from plottr.gui.widgets import PlotWindow


#: The type of a plottr app
AppType = Callable[[Any], Tuple[Flowchart, PlotWindow]]

#: The type of the ids.
IdType = Union[int, str]


logger = log.getLogger(__name__)


# TODO: Check that when the automatic rst is generated, the formatting of the docstrings are correct.
class AppServer(QtCore.QObject):
    """Simple helper object that we can run in a separate thread to listen
    to commands from the manager.

    When the server gets a message, the messageReceived signal gets emitted. Once that happens it will wait until the
    reply variable gets filled with a reply (this is done by triggering the slot loadReply()). After that, sends the
    reply back and resets the reply variable.

    To see the rules of what can be received please see the :obj:App.onMessageReceived. Only exception is if the server
    receives the string "ping", it will immediately reply with the string "pong" without bothering the App.

    There are 2 ways of stopping the server:
        * You can request an interruption of the thread the server is running on.
        * Trigger the quit() slot with a signal.
    Both ways will make the running variable ``False``, and stop the listening loop.
    """

    messageReceived = Signal(object)

    def __init__(self, context: zmq.Context, port: str, parent: Optional[QtCore.QObject] = None):
        """
        Constructor for :class: `.AppServer`

        :param context: The zmq context generated by the app.
        :param port: The port number, in string format, to which to listen to commands.
        :param parent: The parent of the server.
        """
        super().__init__(parent=parent)
        self.port = port
        self.address = '127.0.0.1'
        self.context = context
        self.t_blocking = 1000  # in ms
        self.reply = None
        self.running = True

        self.socket = self.context.socket(zmq.REP)
        self.poller = zmq.Poller()
        self.poller.register(self.socket, zmq.POLLIN)

    def run(self) -> None:
        """
        Connects the socket and starts listening for commands.
        """
        self.socket.bind(f'tcp://{self.address}:{self.port}')

        while self.running:
            # Check if there are any messages.
            evts = self.poller.poll(self.t_blocking)
            if len(evts) > 0:
                message = self.socket.recv_pyobj()
                if message == 'ping':
                    self.socket.send_pyobj('pong')
                else:
                    self.messageReceived.emit(message)
                    # Wait until the reply is generated.
                    while self.reply is None:
                        qtsleep(0.01)
                    self.socket.send_pyobj(self.reply)
                    self.reply = None

            if self.thread().isInterruptionRequested():
                self.running = False
            qtsleep(0.5)

        # When the server is done, close the socket.
        self.socket.close(1)
        self.socket = None

    @Slot()
    def quit(self) -> None:
        """
        Stops the server.
        """
        self.running = False

    @Slot(object)
    def loadReply(self, reply: Any) -> None:
        """
        Slot used to load the reply of a command. Should be connected to a signal that emits the reply.
        """
        self.reply = reply


class App(QtCore.QObject):
    """
    Object that effectively wraps a plottr app.
    Runs an :class:`.AppServer` in a separate thread, which allows to receive and send messages
    from the parent :class:`.AppManager` to the app.
    """
    #: Signal() --  emitted when the app is going to close. Used to close the AppServer
    endProcess = Signal()

    #: Signal(Any) -- emitted when the App has the reply for a message. The AppServer gets the signal and replies.
    #: Arguments:
    #:  Any python object that can be pickled.
    replyReady = Signal(object)

    def __init__(self, setupFunc: AppType, port: int, parent: Optional[QtCore.QObject] = None, *args: Any):
        super().__init__(parent=parent)

        self.fc, self.win = setupFunc(args[0])
        assert isinstance(self.fc, Flowchart)
        assert isinstance(self.win, PlotWindow)
        self.win.show()
        self.win.windowClosed.connect(self.onQuit)

        self.context = zmq.Context()
        self.port = port
        self.server: Optional[AppServer] = AppServer(self.context, str(port))
        self.serverThread: Optional[QtCore.QThread] = QtCore.QThread()

        self.endProcess.connect(self.server.quit)
        self.replyReady.connect(self.server.loadReply)
        self.server.messageReceived.connect(self.onMessageReceived)
        self.server.moveToThread(self.serverThread)
        self.serverThread.started.connect(self.server.run)
        self.serverThread.start()

    @Slot(object)
    def onMessageReceived(self, message: Tuple[str, str, Any]) -> None:
        """
        Handles message reception and reply to the app. Emits the signal replyReady with the reply. The signal is
        connected to the AppServer and the server sends the reply back.

        :param message: Tuple containing 2 strings and an Object.

            * First item, targetName: name of the target object in the app.
                This may be the app :class:`plottr.node.node.Flowchart`
                (on names ```` (empty string), ``fc``, or ``flowchart``;
                or any :class:`plottr.node.node.Node` in the flowchart
                (on name of the node in the app flowchart).

            * Second Item, targetProperty:

                * if the target is a node, then this should be the name of a property
                    of the node.

                * if the target is the flowchart, then ``setInput`` or ``getInput`` are
                    supported as target properties.

            * Thirs Item ,value: a valid value that the target property can be set to.
                for the ``setInput`` option of the flowchart, this should be data, i.e.,
                a dictionary with ``str`` keys and  :class:`plottr.data.datadict.DataDictBase`
                values. Commonly ``{'dataIn': someData}`` for most flowcharts.
                for the ``setInput`` option of the flowchart, this may be any object
                and will be ignored.
        """

        assert self.fc is not None and self.win is not None
        targetName = message[0]
        targetProperty = message[1]
        value = message[2]

        if targetName in ['', 'fc', 'flowchart']:
            if targetProperty == 'setInput':
                reply = self.fc.setInput(**value)
            elif targetProperty == 'getOutput':
                reply = self.fc.outputValues()
            else:
                reply = ValueError(f"Flowchart supports only setting input values ('setInput') "
                                          f"or getting output values ('getOutput'). "
                                          f"'{targetProperty}' is not known.")
        else:
            ret: Union[bool, Exception]
            try:
                node = self.fc.nodes()[targetName]
                setattr(node, targetProperty, value)
                reply = True
            except Exception as e:
                reply = e

        self.replyReady.emit(reply)

    @Slot()
    def onQuit(self) -> None:
        """
        Gets called when win is about to close. Emits endProcess to stop the server. Destroys the zmq context and stops
        the server thread.
        """
        self.endProcess.emit()
        self.context.destroy(1)

        if self.server is not None and self.serverThread is not None:
            self.serverThread.requestInterruption()
            self.serverThread.quit()
            self.serverThread.wait()
            self.server.deleteLater()
            self.serverThread.deleteLater()
            self.serverThread = None
            self.server = None


class ProcessMonitor(QtCore.QObject):
    """
    Helper class that runs in a separate thread. Its job is to constantly check if a process is still running and
    alert the AppManager when a process has been closed and to print any standard output or standard error that
    any process is sending.
    """

    #: Signal(IdType) -- emitted when it detects that a process is closed.
    #: Arguments:
    #:  * The Id of the newly closed process.
    processTerminated = Signal(object)

    def __init__(self, parent: Optional[QtCore.QObject] = None):
        super().__init__(parent=parent)
        self.processes: Dict[IdType, QtCore.QProcess] = {}
        self.checking = True

    @Slot(object, object)
    def onNewProcess(self, Id: IdType, process: QtCore.QProcess) -> None:
        """
        Slot used to add a new process to the ProcessMonitor.

        :param Id: The Id of the process.
        :param process: The QProcess to keep track of.
        """
        self.processes[Id] = process
        self.processes[Id].readyReadStandardOutput.connect(self.onReadyStandardOutput)
        self.processes[Id].readyReadStandardError.connect(self.onReadyStandardError)

    def quit(self) -> None:
        """
        Stops the monitor.
        """
        self.checking = False

    def run(self) -> None:
        """
        Starts the monitor, periodically checks if all the processes are still running. Emits processTerminated with the
        Id of the process that has finished.
        """
        while self.checking:
            processesCopy = self.processes.copy()
            for Id, p in processesCopy.items():
                state = p.state()
                if state == 0:
                    del self.processes[Id]
                    self.processTerminated.emit(Id)
            qtsleep(0.01)

    @Slot()
    def onReadyStandardOutput(self) -> None:
        """
        Gets called when any process emits the readyReadStandardOutput signal, and prints any message it receives.
        """
        for Id, process in self.processes.items():
            output = str(process.readAllStandardOutput(), 'utf-8')  # type: ignore[call-overload] # mypy complains about str() not accepting QbyteArray even though it is an object
            if output != '':
                print(f'Process {Id}: {output}')

    @Slot()
    def onReadyStandardError(self) -> None:
        """
        Gets called when any process emits the readyReadStandardError signal, and prints any messages it receives.
        """
        for Id, process in self.processes.items():
            output = str(process.readAllStandardError(), 'utf-8')  # type: ignore[call-overload] # mypy complains about str() not accepting QbyteArray even though it is an object.
            if output != '':
                print(f'Process {Id}: {output}')


class AppManager(QtWidgets.QWidget):
    """A widget that launches, manages, and communicates with app instances
    that run in separate processes.

    Each app will get assigned a tcp port to use for communication purposes. The first port to be assigned is 12345 by
    default. Every app after the first one will use the next available integer. The manager will reuse a port if an app
    gets closed and frees the port with it.
    """

    #: Signal(IdType, QtCore.QProcess) -- emitted when a new app is created.
    #: Arguments:
    #:  * The app instance id.
    #:  * The QProcess running that app.
    newProcess = Signal(object, object)

    closeProcmon = Signal()

    def __init__(self, initialPort: int = 12345, parent: Optional[QtWidgets.QWidget] = None):
        """
        Constructor of AppManager.

        :param initialPort: The first port to be assigned to the first App.
        """
        super().__init__(parent=parent)
        self.processes: Dict[IdType, Dict[str, Union[QtCore.QProcess, zmq.sugar.socket.Socket, int]]] = {}

        self.context = zmq.Context()
        self.poller = zmq.Poller()
        self.address = '127.0.0.1'
        self.initialPort = initialPort  # This is the port that will be automatically assigned to the next app

        self.procmon: Optional[ProcessMonitor] = ProcessMonitor()
        self.procmonThread: Optional[QtCore.QThread] = QtCore.QThread(parent=self)
        self.procmon.moveToThread(self.procmonThread)
        self.newProcess.connect(self.procmon.onNewProcess)
        self.procmon.processTerminated.connect(self.onProcessEneded)
        self.procmonThread.started.connect(self.procmon.run)
        self.procmonThread.start()

    def launchApp(self, Id: IdType, module: str, func: str, *args: Any) -> bool:
        """
        Launches a new app. If this function does not contain correct arguments (both for this specific function and the
        app launching function, func, the manager will not open anything but will not complain either.
        The rules for what an App is are specified in the docstring of this module.

        :param Id: The Id of an app, this can be an int or a string.
        :param module: The module where the app function lives.
        :param func: The function that opens the app.
        :returns: True if the process has launched successfully, False if not.
        """
        if Id not in self.processes:
            # Find the first available port
            usedPorts = [data['port'] for data in self.processes.values()]
            port = self.initialPort
            while port in usedPorts:
                port += 1

            fullArgs = [str(Path(plottrPath).joinpath('apps', 'apprunner.py')), str(port), module, func] + list(args)
            process = QtCore.QProcess()
            process.start('python', fullArgs)
            process.waitForStarted(100)
            socket = self.context.socket(zmq.REQ)
            socket.connect(f'tcp://{self.address}:{str(port)}')
            self.poller.register(socket, zmq.POLLIN)
            self.processes[Id] = {'process': process,
                                  'port': port,
                                  'socket': socket}
            self.newProcess.emit(Id, process)
            return True

        logger.warning(f'Id {Id} already exists')
        return False

    @Slot(object)
    def onProcessEneded(self, Id: IdType) -> None:
        """
        Gets triggered when the ProcessMonitor detects a process has been closed. Deletes the process from the internal
        dictionary.

        :param Id: The id of the parameter to delete.
        """
        del self.processes[Id]

    def pingApp(self, Id: IdType) -> bool:
        """
        Pings the specified app. If a response is received returns true, False otherwise.

        :param Id: The Id of the app to be pinged.
        :return: True if the ping was successful, False if not.
        """
        if Id not in self.processes:
            logger.warning(f'{Id} not present in the processes.')
            return False
        socket = self.processes[Id]['socket']
        assert isinstance(socket, zmq.sugar.socket.Socket)
        socket.send_pyobj('ping')
        reply = socket.recv_pyobj()
        if reply == 'pong':
            return True
        return False

    def message(self, Id: IdType, targetName: str, targetProperty: str, value: Any) -> Any:
        """Send a message to an app instance.

        :param Id: ID of the app instance.

        :param targetName: Name of the target object in the app.
            This may be the app :class:`plottr.node.node.Flowchart`
            (on names ```` (empty string), ``fc``, or ``flowchart``;
            or any :class:`plottr.node.node.Node` in the flowchart
            (on name of the node in the app flowchart).

        :param targetProperty:

            * If the target is a node, then this should be the name of a property
              of the node.

            * If the target is the flowchart, then ``setInput`` or ``getInput`` are
              supported as target properties.

        :param value: A valid value that the target property can be set to.
            For the ``setInput`` option of the flowchart, this should be data, i.e.,
            a dictionary with ``str`` keys and  :class:`plottr.data.datadict.DataDictBase`
            values. Commonly ``{'dataIn': someData}`` for most flowcharts.
            For the ``setInput`` option of the flowchart, this may be any object
            and will be ignored.

        :returns: the response to the message. Can be:

            *  An exception if the message resulted in an exception being raised.

            * ``True`` if a property was set successfully

            * Data, if flowchart data was requested.

            * ``None``, otherwise.
        """
        if Id not in self.processes:
            raise ValueError(f"no app with ID <{Id}> running.")
        else:
            socket = self.processes[Id]['socket']
            assert isinstance(socket, zmq.sugar.socket.Socket)
            socket.send_pyobj((targetName, targetProperty, value))
            response = socket.recv_pyobj()

        if isinstance(response, Exception):
            logger.warning(f'Exception occurred in app <{Id}>:')
            print_exception(type(response), response, response.__traceback__)

        return response

    def closeEvent(self, a0: QtGui.QCloseEvent) -> None:
        """
        Overwrite of the closeEvent. Makes sure everything closes up properly.
        """
        if self.procmon is not None:
            self.procmon.quit()
            self.procmon.deleteLater()
            self.procmon = None

        if self.procmonThread is not None:
            self.procmonThread.quit()
            self.procmonThread.wait()
            self.procmonThread.deleteLater()
            self.procmonThread = None

        for Id, data in self.processes.items():
            process = data['process']
            assert isinstance(process, QtCore.QProcess)
            process.close()

            socket = data['socket']
            assert isinstance(socket, zmq.sugar.socket.Socket)
            socket.close(1)

        self.context.destroy(1)

        return super().closeEvent(a0)
